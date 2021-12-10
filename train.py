import os
import time
from tqdm import tqdm
import torch
import math

from torch.utils.data import DataLoader
from torch.nn import DataParallel

from nets.attention_model import set_decode_type
from utils.log_utils import log_values
from utils import move_to

from tianshou.data import ReplayBuffer, Batch
from tianshou.policy import SACPolicy
from problems.tsp.state_tsp import StateTSP
from torch.distributions import Categorical
from nets.random_actor import Random_Model


def get_inner_model(model):
    return model.module if isinstance(model, DataParallel) else model


def validate(model, dataset, problem, opts):
    # Validate
    print('Validating...')
    cost = rollout(model, dataset, problem, opts)
    avg_cost = cost.mean()
    print('Validation overall avg_cost: {} +- {}'.format(
        avg_cost, torch.std(cost) / math.sqrt(len(cost))))

    return avg_cost


def rollout(model, dataset, problem, opts):
    # Put in greedy evaluation mode!
    set_decode_type(model, "greedy")
    model.eval()

    def eval_model_bat(bat):
        
        _bat = move_to(bat, opts.device)
        _bat = problem.make_state(_bat)
        
        with torch.no_grad():
            cost = model.solve(_bat, problem, opts)
        return cost.data.cpu()

    return torch.cat([
        eval_model_bat(bat)
        for bat
        in tqdm(DataLoader(dataset, batch_size=opts.eval_batch_size), disable=opts.no_progress_bar)
    ], 0)


def clip_grad_norms(param_groups, max_norm=math.inf):
    """
    Clips the norms for all param groups to max_norm and returns gradient norms before clipping
    :param optimizer:
    :param max_norm:
    :param gradient_norms_log:
    :return: grad_norms, clipped_grad_norms: list with (clipped) gradient norms per group
    """
    grad_norms = [
        torch.nn.utils.clip_grad_norm_(
            group['params'],
            max_norm if max_norm > 0 else math.inf,  # Inf so no clipping but still call to calc
            norm_type=2
        )
        for group in param_groups
    ]
    grad_norms_clipped = [min(g_norm, max_norm) for g_norm in grad_norms] if max_norm > 0 else grad_norms
    return grad_norms, grad_norms_clipped


def train_epoch(model, optimizer, baseline, lr_scheduler, epoch, val_dataset, problem, tb_logger, opts):
    print("Start train epoch {}, lr={} for run {}".format(epoch, optimizer.param_groups[0]['lr'], opts.run_name))
    step = epoch * (opts.epoch_size // opts.batch_size)
    start_time = time.time()

    if not opts.no_tensorboard:
        tb_logger.log_value('learnrate_pg0', optimizer.param_groups[0]['lr'], step)

    # Generate new training data for each epoch
    training_dataset = baseline.wrap_dataset(problem.make_dataset(
        size=opts.graph_size, num_samples=opts.epoch_size, distribution=opts.data_distribution))
    training_dataloader = DataLoader(training_dataset, batch_size=opts.batch_size, num_workers=1)

    # Put model in train mode!
    model.train()
    set_decode_type(model, "sampling")

    for batch_id, batch in enumerate(tqdm(training_dataloader, disable=opts.no_progress_bar)):

        train_batch(
            model,
            optimizer,
            baseline,
            epoch,
            batch_id,
            step,
            batch,
            tb_logger,
            opts
        )

        step += 1

    epoch_duration = time.time() - start_time
    print("Finished epoch {}, took {} s".format(epoch, time.strftime('%H:%M:%S', time.gmtime(epoch_duration))))

    if (opts.checkpoint_epochs != 0 and epoch % opts.checkpoint_epochs == 0) or epoch == opts.n_epochs - 1:
        print('Saving model and state...')
        torch.save(
            {
                'model': get_inner_model(model).state_dict(),
                'optimizer': optimizer.state_dict(),
                'rng_state': torch.get_rng_state(),
                'cuda_rng_state': torch.cuda.get_rng_state_all(),
                'baseline': baseline.state_dict()
            },
            os.path.join(opts.save_dir, 'epoch-{}.pt'.format(epoch))
        )

    avg_reward = validate(model, val_dataset, problem, opts)

    if not opts.no_tensorboard:
        tb_logger.log_value('val_avg_reward', avg_reward, step)

    baseline.epoch_callback(model, epoch)

    # lr_scheduler should be called at end of epoch
    lr_scheduler.step()


# MARK: SAC Functions ////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////

# ReplayBuffer(size=9)
# buf.add(obs=i, act=i, rew=i, done=i, obs_next=i + 1, info={})
def train_epoch_sac(
    actor, # tianshou.policy.BasePolicy (s -> logits)
    actor_optim,
    critic1, # (s, a -> Q(s, a))
    critic1_optim,
    critic2, # (s, a -> Q(s, a))
    critic2_optim,
    value_model,
    v_optimizer,
    buffer: ReplayBuffer, 
    optimizer, baseline, 
    lr_scheduler, 
    epoch, 
    val_dataset, 
    problem, 
    tb_logger, 
    opts
):
    print("Start train epoch {}, lr={} for run {}".format(epoch, optimizer.param_groups[0]['lr'], opts.run_name))
    step = epoch * (opts.epoch_size // opts.batch_size)
    start_time = time.time()

    if not opts.no_tensorboard:
        tb_logger.log_value('learnrate_pg0', optimizer.param_groups[0]['lr'], step)

    # Generate new training data for each epoch
    training_dataset = baseline.wrap_dataset(problem.make_dataset(
        size=opts.graph_size, num_samples=opts.epoch_size, distribution=opts.data_distribution))
    training_dataloader = DataLoader(training_dataset, batch_size=opts.batch_size, num_workers=1)

    # Put model in train mode!
    actor.train() # TODO check if this is necessary
    critic1.train()
    critic2.train()
    value_model.train()
    #sac_model.train()
    set_decode_type(actor, "sampling")

    for batch_id, batch in enumerate(tqdm(training_dataloader, disable=opts.no_progress_bar)):

        collect_experience_sac(
            actor, # todo use random policy or sac model
            buffer,
            problem,
            batch,
            opts
        )

        # NOTES OF MISTAKES:
        # re-sampled actions for value optimization, but need to take those from replay buffer
        # didnt set model.train() or decode type "sampling" after validating
        # replay buffer was too small and only contained experience of almost solved problems
        # state values were mostly positive - entropy was weighted too high, making agent random, ...
        #   ... also q/v-estimator didn't have much possibilities to create negative numbers
        #       changed ReLU in MultiHeadAttentionLayer to leakyReLY
        # changed number of encode_layers in q/v-estimators
        # learning rate decay was never called - currently only for actor!
        # different learning rates for q/v-estimators than for actor used now!

        for i in range(20):
            discount = 1.0
            experience_batch_size = 512
            experience_batch = sample_buffer(buffer, experience_batch_size)
        
            train_batch_sac(
                actor, # tianshou.policy.BasePolicy (s -> logits)
                actor_optim,
                critic1, # (s, a -> Q(s, a))
                critic1_optim,
                critic2, # (s, a -> Q(s, a))
                critic2_optim,
                value_model,
                v_optimizer,
                discount,
                experience_batch,
                opts
            )

        avg_reward = validate(actor, val_dataset, problem, opts)
        #random_reward = validate(Random_Model(), val_dataset, problem, opts)
        actor.train()
        set_decode_type(actor, "sampling")
        lr_scheduler.step() # decreases learning rate (only of the actor currently!!)

        step += 1

    epoch_duration = time.time() - start_time
    print("Finished epoch {}, took {} s".format(epoch, time.strftime('%H:%M:%S', time.gmtime(epoch_duration))))

    if (opts.checkpoint_epochs != 0 and epoch % opts.checkpoint_epochs == 0) or epoch == opts.n_epochs - 1:
        print('Saving model and state...')
        torch.save(
            {
                'model': get_inner_model(actor).state_dict(),
                'optimizer': optimizer.state_dict(),
                'rng_state': torch.get_rng_state(),
                'cuda_rng_state': torch.cuda.get_rng_state_all(),
                'baseline': baseline.state_dict()
            },
            os.path.join(opts.save_dir, 'epoch-{}.pt'.format(epoch))
        )

    avg_reward = validate(actor, val_dataset, problem, opts)

    if not opts.no_tensorboard:
        tb_logger.log_value('val_avg_reward', avg_reward, step)

    baseline.epoch_callback(actor, epoch)


def sample_buffer(buffer: ReplayBuffer, count):
    batch, ids = buffer.sample(batch_size=count)
    return batch


def collect_experience_sac(
        model,
        buffer: ReplayBuffer,
        problem,
        input_batch,
        opts
):
    x = input_batch # TODO remove baseline stuff from batch - not needed anymore in sac - some baselines use batch['data'] here
    x = move_to(x, opts.device)

    prev_state = problem.make_state(x)
    #prev_state.loc = move_to(prev_state.loc, opts.device)

    j = 0
    while not prev_state.all_finished():
        # Input to model will not be just a graph anymore, but rather an observation -> partially solved graph, i.e. state
        # try to input a tianshou batch here instead of a tsp state
        # first try to only use the state, without x, since x should be in state loc
        logits, _ = model(prev_state)
        # logits = torch.tensor(logits, device=opts.device) # TODO hacky for now because exp only works with tensors but tianshou wants tuples
        # logits.exp() gives probabilities
        mask = prev_state.get_mask()
        selected = model._select_node(logits.exp(), mask[:, 0, :])
        state = prev_state.update(selected)
        cost = problem.get_step_cost(prev_state, state, opts)

        for i in range(x.shape[0]): # iterate over the whole batch and save each last step to replay buffer
            s = prev_state[torch.tensor(i)]
            s_next = state[torch.tensor(i)]

            # Note: batch expects tensors at the leaves. converting states to dicts with tensors at leaves
            # see https://tianshou.readthedocs.io/en/master/tutorials/batch.html
            obs = s.asdict()
            obs_next = s_next.asdict()

            b = Batch(obs=obs, act=s_next.prev_a, rew=-cost[i].item(), done=s_next.all_finished(), obs_next=obs_next, info={})
            # buffer doesnt like getting None once and then actual elements later
            buffer.add(b)

        prev_state = state
        j += 1


def train_batch_sac(
        actor, # tianshou.policy.BasePolicy (s -> logits)
        actor_optim,
        critic1, # (s, a -> Q(s, a))
        critic1_optim,
        critic2, # (s, a -> Q(s, a))
        critic2_optim,
        value_model,
        v_optimizer,
        discount,
        experience_batch: Batch,
        opts
):
    # tianshou issues
    # sac_model.learn(experience_batch) # somehow cant deal with actual batches
    # somehow tianshou expects only single elements in forward
    # also wants logits as tuple for some reason
    # also creates a distribution using this tuple, which probably fails because logits tuple has wrong format
    # (is trying to unpack in the Normal constructor parameters)

    logits, _ = actor(experience_batch.obs)
    
    current_q1 = critic1(experience_batch.obs)
    current_q2 = critic2(experience_batch.obs)

    current_v = value_model(experience_batch.obs).flatten()
    next_v = value_model(experience_batch.obs_next).flatten()
    # print(next_v) # viel positiv :O - vermutlich wegen hohem entropy faktor - wird deshalb random?
    # note: i currently can't backpropagate to value network parameters !!! how do i do this?
    # wait i do ! i calculate difference in loss lol

    # still viel POSITIV! - wie kann das sein? loss ist bei q-values und values gar nicht so schlecht ...




# LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE
# LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE
# LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE
# LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE
# LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE
# LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE
# LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE
# LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE
# LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE
# LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE
# LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE
# LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE
# LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE
# LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE - LOOK ABOVE



    current_act_q1 = current_q1.gather(1, experience_batch.act)
    current_act_q2 = current_q2.gather(1, experience_batch.act)
    act_logits = logits.gather(1, experience_batch.act)

    act_log_probs = torch.log(act_logits.exp())

    current_rewards = torch.tensor(experience_batch.rew).to(opts.device).flatten() # TODO dont use tianshou batch, so i dont have to move back and fourth between cpu and gpu
    alpha = 0.002 # entropy term becomes quite large wrt. q-values

    value_target = torch.minimum(current_act_q1, current_act_q2).detach() - alpha * act_log_probs.detach()
    q_target = discount * (current_rewards + next_v.detach())

    value_loss = torch.square(current_v - value_target).mean() # torch.square
    q1_loss = torch.square(current_act_q1 - q_target).mean()
    q2_loss = torch.square(current_act_q2 - q_target).mean()

    # actor loss computation
    softy = torch.nn.Softmax(dim=1)
    soft_q1 = softy(current_q1.detach()) + 1e-6
    soft_actor = softy(logits) + 1e-6 # prevent nan when calculating log below
    p_div_q = torch.div(soft_actor, soft_q1)
    log_p_div_q = torch.log(p_div_q)
    p_log_p_div_q = torch.mul(soft_actor, log_p_div_q)
    kl = torch.sum(p_log_p_div_q, dim=1)
    actor_loss = torch.mean(kl)

    # optimization
    v_optimizer.zero_grad()
    value_loss.backward()
    v_optimizer.step()

    critic1_optim.zero_grad()
    q1_loss.backward()
    critic1_optim.step()

    critic2_optim.zero_grad()
    q2_loss.backward()
    critic2_optim.step()

    actor_optim.zero_grad()
    actor_loss.backward()
    actor_optim.step()

    # q1_loss, q2_loss, value_loss, actor_loss
    print(f"LOSSES - Actor: {actor_loss.item()}, Q1: {q1_loss.item()}, Q2: {q2_loss.item()}, V: {value_loss.item()}")


def train_batch(
        model,
        optimizer,
        baseline,
        epoch,
        batch_id,
        step,
        batch,
        tb_logger,
        opts
):
    # TODO
    # sample batch from Replay Buffer and feed it into this function
    # adjust model(x) to work on batches of individual items from the replay buffer
    #   (only predict one step, then update models, add new experience to RB)
    #   Note: the decoder needs all of the input embeddings, this is quite inefficient, as we run per sample (maybe save it in the experience)
    # introduce Q and state value estimator and learn policy using KL divergence -> this can be done using tianshou SAC, right?
    #   the estimators will need some form of masking, might be hard to introduce
    # 

    x, bl_val = baseline.unwrap_batch(batch)
    print("---------") 
    print(x.shape) # 512, n, 2 -> 512 items, n nodes and 2 coordinates

    x = move_to(x, opts.device)
    bl_val = move_to(bl_val, opts.device) if bl_val is not None else None

    # Evaluate model, get costs and log probabilities
    cost, log_likelihood = model(x)
    print("-----")
    print(cost.shape) # one batch of total costs of each trajectory
    print(log_likelihood.shape) # one batch of probabilities of each trajectory

    # Evaluate baseline, get baseline loss if any (only for critic)
    bl_val, bl_loss = baseline.eval(x, cost) if bl_val is None else (bl_val, 0)
    print("---")
    print(bl_val) # 1 value
    print(bl_loss) # 1 value

    # Calculate loss
    reinforce_loss = ((cost - bl_val) * log_likelihood).mean()
    loss = reinforce_loss + bl_loss

    # Perform backward pass and optimization step
    optimizer.zero_grad()
    loss.backward()
    # Clip gradient norms and get (clipped) gradient norms for logging
    grad_norms = clip_grad_norms(optimizer.param_groups, opts.max_grad_norm)
    optimizer.step()

    # Logging
    if step % int(opts.log_step) == 0:
        log_values(cost, grad_norms, epoch, batch_id, step,
                   log_likelihood, reinforce_loss, bl_loss, tb_logger, opts)
