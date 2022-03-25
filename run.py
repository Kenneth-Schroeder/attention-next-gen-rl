#!/usr/bin/env python

import pprint as pp

import torch
import torch.optim as optim

from options import get_options
from nets.attention_model import AttentionModel
from nets.v_estimator import V_Estimator
from utils import load_problem

import tianshou as ts
from problems.tsp.tsp_env import TSP_env
from problems.op.op_env import OP_env
from torch.utils.tensorboard import SummaryWriter
from tianshou.utils import TensorboardLogger
from torch.optim.lr_scheduler import ExponentialLR

import numpy as np
from nets.argmaxembed import ArgMaxEmbed

class Categorical_logits(torch.distributions.categorical.Categorical):
    def __init__(self, logits, validate_args=None):
        super(Categorical_logits, self).__init__(logits=logits, validate_args=validate_args)


def updatelog_eps_lr(policy, eps, logger, epoch, lr_scheduler=None, step=None, batch_size=None, log=False):
    policy.set_eps(eps)
    if log:
        logger.write("train/epsilon", epoch, {'Epsilon':eps})
    if lr_scheduler is not None:
        lr_scheduler.step()
        if log:
            logger.write("train/lr", step*batch_size, {'LR':lr_scheduler.get_last_lr()[0]})


def run_DQN(opts, logger):
    problem = load_problem(opts.problem)
    problem_env_class = { 'tsp': TSP_env, 'op': OP_env }

    actor = AttentionModel(
        opts.embedding_dim,
        opts.hidden_dim,
        problem,
        output_probs=False,
        n_encode_layers=opts.n_encode_layers,
        mask_inner=True,
        mask_logits=True,
        normalization=opts.normalization,
        tanh_clipping=opts.tanh_clipping
    ).to(opts.device)

    # https://discuss.pytorch.org/t/how-to-optimize-multi-models-parameter-in-one-optimizer/3603/6
    learning_rate = 1e-3
    optimizer = optim.Adam([
        {'params': actor.parameters(), 'lr': learning_rate}
    ])

    lr_scheduler = ExponentialLR(optimizer, gamma=0.9997, verbose=False)

    num_epochs = 500
    
    num_train_envs = 32 # has to be smaller or equal to episode_per_collect
    episode_per_collect = num_of_buffer = num_train_envs # they can't differ, or VectorReplayBuffer will introduce bad data to training
    
    batch_size = opts.graph_size * episode_per_collect # has to be smaller or equal to buffer_size, defines minibatch size in policy training
    buffer_size = batch_size * 20
    

    step_per_epoch = batch_size * 100

    num_test_envs = 1024 # has to be smaller or equal to num_test_episodes
    num_test_episodes = 1024 # just collect this many episodes using policy and checks the performance
    step_per_collect = batch_size

    train_envs = ts.env.DummyVectorEnv([lambda: problem_env_class[opts.problem](opts) for _ in range(num_train_envs)])
    test_envs = ts.env.DummyVectorEnv([lambda: problem_env_class[opts.problem](opts) for _ in range(num_test_envs)])

    gamma, n_step, target_freq = 1.00, 1, 100
    policy = ts.policy.DQNPolicy(actor, optimizer, gamma, n_step, target_update_freq=target_freq)
    eps_train, eps_test = 0.5, 0.2

    logger.writer.add_text("hyperparameters", f"{learning_rate=}, {episode_per_collect=}, {batch_size=}, \
        {step_per_epoch=}, {num_test_envs=}, {n_step=}, {target_freq=}, {eps_train=}, {eps_test=}")

    replay_buffer = ts.data.VectorReplayBuffer(total_size=buffer_size, buffer_num=num_of_buffer)
    train_collector = ts.data.Collector(policy, train_envs, replay_buffer, exploration_noise=False)
    test_collector = ts.data.Collector(policy, test_envs, exploration_noise=False)

    result = ts.trainer.offpolicy_trainer( # DOESN'T work with PPO, which makes sense
        policy, train_collector, test_collector, num_epochs, step_per_epoch, step_per_collect,
        num_test_episodes, batch_size, update_per_step=1 / step_per_collect,
        train_fn=lambda epoch, env_step: updatelog_eps_lr(policy, eps_train/(epoch+1), logger, epoch, lr_scheduler=lr_scheduler, step=env_step, batch_size=batch_size, log=True),
        test_fn=lambda epoch, env_step: updatelog_eps_lr(policy, eps_train/(epoch+1), logger, epoch, log=False),
        #stop_fn=lambda mean_rewards: mean_rewards >= env.spec.reward_threshold,
        logger=logger
    )

    torch.save(policy.state_dict(), f"policy_dir/{opts.run_name}.pth")
    #policy.load_state_dict(torch.load("policy.pth"))


def run_PG(opts, logger):
    problem = load_problem(opts.problem)
    problem_env_class = { 'tsp': TSP_env, 'op': OP_env }

    actor = AttentionModel(
        opts.embedding_dim,
        opts.hidden_dim,
        problem,
        output_probs=False,
        n_encode_layers=opts.n_encode_layers,
        mask_inner=True,
        mask_logits=True,
        normalization=opts.normalization,
        tanh_clipping=opts.tanh_clipping
    ).to(opts.device)

    # https://discuss.pytorch.org/t/how-to-optimize-multi-models-parameter-in-one-optimizer/3603/6
    learning_rate = 1e-4
    optimizer = optim.Adam([
        {'params': actor.parameters(), 'lr': learning_rate},
    ])

    lr_scheduler = ExponentialLR(optimizer, gamma=0.9997, verbose=False)

    num_epochs = 500
    
    num_train_envs = 32 # has to be smaller or equal to episode_per_collect
    episode_per_collect = num_of_buffer = num_train_envs # they can't differ, or VectorReplayBuffer will introduce bad data to training
    
    batch_size = opts.graph_size * episode_per_collect # has to be smaller or equal to buffer_size, defines minibatch size in policy training
    buffer_size = batch_size # doesn't make a lot of sense to multiply here for onpolicy?
    
    repeat_per_collect = 1 # how many times to learn each batch
    step_per_epoch = batch_size * 100

    num_test_envs = 1024 # has to be smaller or equal to num_test_episodes
    num_test_episodes = 1024 # just collect this many episodes using policy and checks the performance

    train_envs = ts.env.DummyVectorEnv([lambda: problem_env_class[opts.problem](opts) for _ in range(num_train_envs)]) #DummyVectorEnv, SubprocVectorEnv
    test_envs = ts.env.DummyVectorEnv([lambda: problem_env_class[opts.problem](opts) for _ in range(num_test_envs)])
    gamma = 1.00

    logger.writer.add_text("hyperparameters", f"{learning_rate=}, {episode_per_collect=}, {batch_size=}, \
        {step_per_epoch=}, {num_test_envs=}, {repeat_per_collect=}")

    distribution_type = Categorical_logits
    policy = ts.policy.PGPolicy(model=actor,
                                optim=optimizer,
                                dist_fn=distribution_type,
                                discount_factor=gamma,
                                lr_scheduler=lr_scheduler, # updates LR each policy update => with each batch
                                reward_normalization=False,
                                deterministic_eval=False)

    replay_buffer = ts.data.VectorReplayBuffer(total_size=buffer_size, buffer_num=num_of_buffer)
    train_collector = ts.data.Collector(policy, train_envs, replay_buffer, exploration_noise=False)
    test_collector = ts.data.Collector(policy, test_envs, exploration_noise=False)

    result = ts.trainer.onpolicy_trainer(
        policy=policy,
        train_collector=train_collector,
        test_collector=test_collector,
        max_epoch=num_epochs,
        step_per_epoch=step_per_epoch,
        repeat_per_collect=repeat_per_collect,
        episode_per_test=num_test_episodes,
        batch_size=batch_size,
        episode_per_collect=episode_per_collect,
        train_fn=lambda epoch, env_step: logger.write("train/learning_rate", epoch, {'LR':lr_scheduler.get_last_lr()[0]}),
        logger=logger
    )

    torch.save(policy.state_dict(), f"policy_dir/{opts.run_name}.pth")


def run_PPO(opts, logger):
    problem = load_problem(opts.problem)
    problem_env_class = { 'tsp': TSP_env, 'op': OP_env }

    actor = AttentionModel(
        opts.embedding_dim,
        opts.hidden_dim,
        problem,
        output_probs=False,
        n_encode_layers=opts.n_encode_layers,
        mask_inner=True,
        mask_logits=True,
        normalization=opts.normalization,
        tanh_clipping=opts.tanh_clipping
    ).to(opts.device)

    critic = V_Estimator(embedding_dim=64, problem=problem).to(opts.device)
    # https://discuss.pytorch.org/t/how-to-optimize-multi-models-parameter-in-one-optimizer/3603/6
    lr_actor = 1e-4
    lr_critic = 1e-5
    optimizer = optim.Adam([
        {'params': actor.parameters(), 'lr': lr_actor},
        {'params': critic.parameters(), 'lr': lr_critic}
    ])

    lr_scheduler = ExponentialLR(optimizer, gamma=0.9997, verbose=False)

    num_epochs = 500
    
    num_train_envs = 32 # has to be smaller or equal to episode_per_collect
    episode_per_collect = num_of_buffer = num_train_envs # they (num_of_buffer and num_train_envs) can't differ, or VectorReplayBuffer will introduce bad data to training
    
    batch_size = opts.graph_size * episode_per_collect # has to be smaller or equal to buffer_size, defines minibatch size in policy training
    buffer_size = batch_size # doesn't make a lot of sense to multiply here for onpolicy?

    repeat_per_collect = 1 
    step_per_epoch = buffer_size * 100

    num_test_envs = 1024 # has to be smaller or equal to num_test_episodes
    num_test_episodes = 1024 # just collect this many episodes using policy and checks the performance

    train_envs = ts.env.DummyVectorEnv([lambda: problem_env_class[opts.problem](opts) for _ in range(num_train_envs)]) #DummyVectorEnv, SubprocVectorEnv
    test_envs = ts.env.DummyVectorEnv([lambda: problem_env_class[opts.problem](opts) for _ in range(num_test_envs)])
    gamma = 1.00

    eps_clip, vf_coef, ent_coef, gae_lambda = 0.2, 0.5, 0.01, 1.00

    logger.writer.add_text("hyperparameters", f"{lr_actor=}, {lr_critic=}, {episode_per_collect=}, {batch_size=}, \
        {step_per_epoch=}, {num_test_envs=}, {repeat_per_collect=}, {eps_clip=}, {vf_coef=}, {ent_coef=}, {gae_lambda=}")

    distribution_type = Categorical_logits
    policy = ts.policy.PPOPolicy(actor=actor,
                                 critic=critic,
                                 optim=optimizer,
                                 dist_fn=distribution_type,
                                 discount_factor=gamma,
                                 lr_scheduler=lr_scheduler,
                                 eps_clip=eps_clip,
                                 dual_clip=None,
                                 value_clip=False,
                                 advantage_normalization=False,
                                 vf_coef=vf_coef,
                                 ent_coef=ent_coef,
                                 gae_lambda=gae_lambda,
                                 reward_normalization=False,
                                 deterministic_eval=False)

    replay_buffer = ts.data.VectorReplayBuffer(total_size=buffer_size, buffer_num=num_of_buffer)
    train_collector = ts.data.Collector(policy, train_envs, replay_buffer, exploration_noise=False)
    test_collector = ts.data.Collector(policy, test_envs, exploration_noise=False)

    result = ts.trainer.onpolicy_trainer(
        policy=policy,
        train_collector=train_collector,
        test_collector=test_collector,
        max_epoch=num_epochs,
        step_per_epoch=step_per_epoch,
        repeat_per_collect=repeat_per_collect,
        episode_per_test=num_test_episodes,
        batch_size=batch_size,
        episode_per_collect=episode_per_collect,
        logger=logger,
        train_fn=lambda epoch, env_step: logger.write("train/learning_rate", epoch, {'LR':lr_scheduler.get_last_lr()[0]})
    )

    torch.save(policy.state_dict(), f"policy_dir/{opts.run_name}.pth")


def run_SAC(opts, logger):
    problem = load_problem(opts.problem)
    problem_env_class = { 'tsp': TSP_env, 'op': OP_env }

    actor = AttentionModel(
        opts.embedding_dim,
        opts.hidden_dim,
        problem,
        output_probs=False,
        n_encode_layers=opts.n_encode_layers,
        mask_inner=True,
        mask_logits=True,
        normalization=opts.normalization,
        tanh_clipping=opts.tanh_clipping
    ).to(opts.device)

    # https://discuss.pytorch.org/t/how-to-optimize-multi-models-parameter-in-one-optimizer/3603/6
    lr_actor, lr_critic1, lr_critic2 = 1e-4, 1e-5, 1e-5
    actor_optimizer = optim.Adam([
        {'params': actor.parameters(), 'lr': lr_actor}
    ])

    critic1 = V_Estimator(embedding_dim=64, q_outputs=True, problem=problem).to(opts.device)
    critic1_optimizer = optim.Adam([
        {'params': critic1.parameters(), 'lr': lr_critic1}
    ])

    critic2 = V_Estimator(embedding_dim=64, q_outputs=True, problem=problem).to(opts.device)
    critic2_optimizer = optim.Adam([
        {'params': critic2.parameters(), 'lr': lr_critic2}
    ])

    num_epochs = 500
    
    num_train_envs = 32 # has to be smaller or equal to episode_per_collect
    episode_per_collect = num_of_buffer = num_train_envs # they can't differ, or VectorReplayBuffer will introduce bad data to training
    
    batch_size = opts.graph_size * episode_per_collect # has to be smaller or equal to buffer_size, defines minibatch size in policy training
    buffer_size = batch_size # doesn't make a lot of sense to multiply here for onpolicy?

    step_per_epoch = buffer_size * 100

    num_test_envs = 1024 # has to be smaller or equal to num_test_episodes
    num_test_episodes = 1024 # just collect this many episodes using policy and checks the performance
    step_per_collect = buffer_size

    train_envs = ts.env.DummyVectorEnv([lambda: problem_env_class[opts.problem](opts) for _ in range(num_train_envs)])
    test_envs = ts.env.DummyVectorEnv([lambda: problem_env_class[opts.problem](opts) for _ in range(num_test_envs)])
    gamma = 1.00
    tau, alpha = 0.005, None

    if alpha == None:
        dummy_env = problem_env_class[opts.problem](opts)
        #test_vec = np.array([0.01, 0.81, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01])
        #log_vec = np.log(test_vec)
        #ent = np.sum(np.multiply(test_vec, log_vec))
        #print(ent)
        #print(dummy_env.action_space)
        #print(-np.prod(dummy_env.action_space.shape))
        target_entropy = -np.prod(dummy_env.action_space.shape)
        log_alpha = torch.zeros(1, requires_grad=True, device=opts.device)
        alpha_optim = torch.optim.Adam([log_alpha], lr=3e-4)
        alpha = (target_entropy, log_alpha, alpha_optim)

    logger.writer.add_text("hyperparameters", f"{lr_actor=}, {lr_critic1=}, {lr_critic2=}, {episode_per_collect=}, {batch_size=}, \
        {step_per_epoch=}, {num_test_envs=}, {tau=}, {alpha=}")

    policy = ts.policy.DiscreteSACPolicy(actor=actor, 
                                         actor_optim=actor_optimizer,
                                         critic1=critic1,
                                         critic2=critic2,
                                         critic1_optim=critic1_optimizer,
                                         critic2_optim=critic2_optimizer,
                                         tau=tau,
                                         gamma=gamma,
                                         alpha=alpha,
                                         exploration_noise=None,
                                         reward_normalization=False,
                                         deterministic_eval=False)

    replay_buffer = ts.data.VectorReplayBuffer(total_size=buffer_size, buffer_num=num_of_buffer)
    train_collector = ts.data.Collector(policy, train_envs, replay_buffer, exploration_noise=False)
    test_collector = ts.data.Collector(policy, test_envs, exploration_noise=False)

    result = ts.trainer.offpolicy_trainer(
        policy, train_collector, test_collector, num_epochs, step_per_epoch, step_per_collect,
        num_test_episodes, batch_size, update_per_step=1 / step_per_collect,
        logger=logger
    )

    torch.save(policy.state_dict(), f"policy_dir/{opts.run_name}.pth")







def batchify_obs(obs):
    obs['loc'] = torch.unsqueeze(obs['loc'], dim=0)
    obs['dist'] = torch.unsqueeze(obs['dist'], dim=0)
    obs['first_a'] = torch.unsqueeze(obs['first_a'], dim=0)
    obs['prev_a'] = torch.unsqueeze(obs['prev_a'], dim=0)
    obs['visited'] = torch.unsqueeze(obs['visited'], dim=0)
    obs['length'] = torch.unsqueeze(obs['length'], dim=0)
    return obs

def run_STE_argmax(opts):
    problem = load_problem(opts.problem)

    model = AttentionModel(
        opts.embedding_dim,
        opts.hidden_dim,
        problem,
        output_probs=False,
        n_encode_layers=opts.n_encode_layers,
        mask_inner=True,
        mask_logits=True,
        normalization=opts.normalization,
        tanh_clipping=opts.tanh_clipping
    ).to(opts.device)

    optimizer = optim.Adam([
        {'params': model.parameters(), 'lr': opts.lr_model},
    ])

    env = TSP_env(opts)
    obs = env.reset()
    done = False

    for epoch_idx in range(10):
        epoch_costs = 0
        for _ in range(opts.epoch_size):
            costs = []
            for _ in range(opts.batch_size):
                total_cost = 0
                obs = batchify_obs(obs)
                node_embeddings = model.encode(obs)

                while not done:
                    logits, _ = model.decode(obs, node_embeddings)
                    
                    # create class to make a differentiable argmax operation with embedding selection - done
                    # adjust env to save those embeddings with grads of the whole trajectory - done 
                    # adjust observation to include the right embeddings - done
                    # adjust network to use these embeddings for context creation - done
                    # maybe adjust network to run encoder only once - done
                    # split model into encoder and decoder for easier use here - done
                    # done?

                    action, action_embedding = ArgMaxEmbed.apply(logits, node_embeddings.squeeze()) # NOTE: because of the squeeze here this doesnt work for batches!
                    obs, reward, done, info = env.step(action, action_embedding)
                    obs = batchify_obs(obs)

                    total_cost += reward
                obs, done = env.reset(), False

                costs.append(total_cost)

            # calculate total cost
            costs = torch.tensor(costs, device=opts.device)
            loss = -costs.mean()
            epoch_costs += costs.mean()

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            print(costs.mean())
            #print(loss)
        print(f'Epoch {epoch_idx} Costs: {epoch_costs/opts.epoch_size}')

def manual_testing(opts):
    problem_env_class = { 'tsp': TSP_env, 'op': OP_env }
    env = problem_env_class[opts.problem](opts)
    obs = env.reset()
    done = False

    print(f"{obs=}")
    while not done:
        obs, reward, done, info = env.step(int(input()))
        print(f"{obs=}, {reward=}, {done=}")


def train(opts):
    writer = SummaryWriter(f"log_dir/{opts.run_name}")
    writer.add_text("args", str(opts))
    logger = TensorboardLogger(writer)

    # Figure out what's the problem
    #run_STE_argmax(opts)
    run_DQN(opts, logger)
    #run_PG(opts, logger)
    #run_PPO(opts, logger)
    #run_SAC(opts, logger) # exploding losses problem? maybe check gradient clipping

    #manual_testing(opts)
    



def run(opts):

    # Pretty print the run args
    pp.pprint(vars(opts))

    # Set the random seed
    #torch.manual_seed(opts.seed)

    # Set the device
    opts.device = torch.device("cuda:0" if opts.use_cuda else "cpu")

    train(opts)


if __name__ == "__main__":
    run(get_options())
