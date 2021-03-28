import pickle
import time
from functools import lru_cache
from collections import OrderedDict
from typing import List, Optional, Tuple, Dict, Sequence, Any

import gym
import gym_nav
import numpy as np
import torch
from stable_baselines3 import PPO
from tqdm import tqdm

import pytorch_util as ptu
import utils
from utils import PathDict
from gcl.agents.base_policy import BasePolicy
from logger import Logger

# set overflow warning to error instead
np.seterr(all='raise')
torch.autograd.set_detect_anomaly(True)

# how many rollouts to save as videos to tensorboard
MAX_NVIDEO = 2
MAX_VIDEO_LEN = 40  # we overwrite this in the code below


class GCL_Trainer(object):
    """ GCL_Trainer """

    def __init__(self, params: dict):

        #############
        # INIT
        #############

        # Get params, create logger
        self.params = params
        self.logger = Logger(self.params['logdir'])

        # Set random seeds
        seed = self.params['seed']
        np.random.seed(seed)
        torch.manual_seed(seed)
        torch.cuda.manual_seed(seed)

        # init gpu
        ptu.init_gpu(
            use_gpu=not self.params['no_gpu'],
            gpu_id=self.params['which_gpu']
        )

        #############
        # ENV
        #############

        # Make the gym environment
        self.env = gym.make(self.params['env_name'])
        self.env.seed(seed)

        # Maximum length for episodes
        self.params['ep_len'] = self.env.max_steps  # TODO: may need to change this for different ENV
        global MAX_VIDEO_LEN
        MAX_VIDEO_LEN = self.params['ep_len']

        # Is this env continuous, or self.discrete?
        discrete = isinstance(self.env.action_space, gym.spaces.Discrete)
        self.params['agent_params']['discrete'] = discrete

        # Are the observations images?
        is_img = len(self.env.observation_space.shape) > 2

        # Observation and action sizes
        ob_dim = self.env.observation_space.shape if is_img else self.env.observation_space.shape[0]
        ac_dim = self.env.action_space.n if discrete else self.env.action_space.shape[0]
        self.params['agent_params']['ac_dim'] = ac_dim
        self.params['agent_params']['ob_dim'] = ob_dim

        # simulation timestep, will be used for video saving
        # Frame Rate
        if 'model' in dir(self.env):
            self.fps = 1/self.env.model.opt.timestep
        elif 'env_wrappers' in self.params:
            self.fps = 30  # This is not actually used when using the Monitor wrapper
        # elif 'video.frames_per_second' in self.env.env.metadata.keys():
        #     self.fps = self.env.env.metadata['video.frames_per_second']
        else:
            self.fps = 10

        # Init total ENV steps and initial_return
        self.total_envsteps = None
        self.initial_return = None

        # Timer
        self.start_time = None

        # Logging Flag
        self.log_video = None
        self.log_metrics = None

        #############
        # AGENT
        #############
        agent_class = self.params['agent_class']
        self.agent = agent_class(self.env, self.params['agent_params'])

    ############################################################################################
    def run_training_loop(self, n_iter: int,
                          collect_policy, eval_policy,
                          expert_data=None, expert_policy=None) -> Tuple[Any, Any]:
        """
        Perform Algorithm 1 Guided cost learning
        :param n_iter: number of iterations
        :param collect_policy: q_k
        :param eval_policy: q_k at t
        :param expert_data: D_demo
        :param expert_policy: pi*
        """
        # Init vars at beginning of training
        self.total_envsteps: int = 0
        self.start_time: float = time.time()

        train_log_lst, policy_log_lst = [], []
        demo_paths: List[PathDict]
        samp_paths: List[PathDict]

        #####################################################################
        # 1. Add demonstrations to replay buffer
        demo_paths, _, _ = self.collect_demo_trajectories(expert_data, expert_policy,
                                                          ntrajs=self.params['demo_size'],
                                                          render=False, verbose=True)
        self.agent.add_to_buffer(demo_paths, demo=True)
        print(f'\nNum of Demo rollouts collected:{self.agent.demo_buffer._num_paths}')
        print(f'Num of Demo transition steps collected:{self.agent.demo_buffer._num_data}')
        utils.toc(self.start_time, "Finish Loading Expert Demonstrations")

        #####################################################################
        # 2.
        n_iter_loop = tqdm(range(n_iter), desc="Guided Cost Learning", leave=False)
        for itr in n_iter_loop:
            print(f"\n********** Iteration {itr} ************")

            # TODO: not log for now, uncomment this and delete follow
            self.log_video = False
            self.log_metrics = False
            # decide if videos should be rendered/logged at this iteration
            # if itr % self.params['video_log_freq'] == 0 and self.params['video_log_freq'] != -1:
            #     self.log_video = True
            # else:
            #     self.log_video = False
            #
            # # decide if metrics should be logged
            # if self.params['scalar_log_freq'] == -1:
            #     self.log_metrics = False
            # elif itr % self.params['scalar_log_freq'] == 0:
            #     self.log_metrics = True
            # else:
            #     self.log_metrics = False

            # 3. Generate fresh samples D_traj from current trajectory distribution q_k (collect_policy)
            # collect trajectories, to be used for training
            # On-policy PG need to collect new trajectories at *every* iteration
            with torch.no_grad():

                training_returns = self.collect_training_trajectories(
                    collect_policy, self.params['sample_size']
                )
            samp_paths, envsteps_this_batch, train_video_paths = training_returns
            # self.total_envsteps += envsteps_this_batch

            # 4. Append samples D_traj to D_samp
            self.agent.add_to_buffer(samp_paths)
            self.buffer_status()

            # 5. Use D_{samp} to update cost c_{\theta}
            reward_logs = self.train_reward()  # Algorithm 2

            # 6. Update q_k(\tau) using D_{traj} and using GPS or PG
            policy_logs = self.train_policy()

            # log/save
            if self.log_video or self.log_metrics:
                # perform logging
                print('\nBeginning logging procedure...')
                # self.perform_logging(itr, paths, eval_policy, train_video_paths, reward_logs, policy_logs)

                if self.params['save_params']:
                    # self.agent.save('{}/agent_itr_{}.pt'.format(self.params['logdir'], itr))
                    pass

            for r, p in zip(reward_logs, policy_logs):
                reward_loss = float(r['Training reward loss'])
                train_log_lst.append(reward_loss)
                policy_loss = float(p["Training_Loss"])
                policy_log_lst.append(policy_loss)

            # update progress bar
            n_iter_loop.set_postfix()
        return train_log_lst, policy_log_lst

    ############################################################################################
    @lru_cache(maxsize=3)
    def collect_demo_trajectories(self,
                                  expert_data: Optional[str] = None, expert_policy: Optional[str] = None,
                                  ntrajs: int = 100, demo_batch_size: int = 1000,
                                  render=False, verbose=False
                                  ) -> Tuple[List[PathDict], int, Any]:
        """
        :param: expert_data:  relative path to saved
        :param: expert_policy:  relative path to saved expert policy
        :param: render: show video of demo trajs
        :param: verbose: evaluate expert policy and print metrics
        :return:
            paths: a list of trajectories with len = self.params['demo_size']
                    each trajectory is a dict {obs, image_obs, acs, log_probs, rewards, next_obs, terminals}
        """
        assert not (expert_data and expert_policy), "Choose either expert_data or expert_policy"
        # Init var
        render_mode = 'human' if render else 'rgb_array'
        envsteps_this_batch = 0
        train_video_paths = None
        demo_paths: List[PathDict]

        # Load expert policy or expert demonstrations D_demo
        if expert_data:
            print('\nLoading saved demonstrations...')
            with open(expert_data, 'rb') as f:
                # TODO: load data may not through pickle
                demo_paths = pickle.load(f)
            # TODO: sample self.params['demo_size'] from demo_paths
            return demo_paths[: ntrajs], 0, None

        elif expert_policy:
            expert_policy_model = PPO.load(expert_policy)
            print('\nRunning expert policy to collect demonstrations...')

            demo_paths = utils.sample_n_trajectories(
                self.env,
                policy=expert_policy_model,
                agent=self.agent,
                ntrajs=self.params['demo_size'],
                max_path_length=self.params['ep_len'],
                render=render,
                render_mode=render_mode,
                expert=True
            )

            if verbose:
                utils.evaluate_model(self.params['env_name'], expert_policy_model, num_episodes=100)
        else:
            raise ValueError('Please provide either expert demonstrations or expert policy')
        return demo_paths, envsteps_this_batch, train_video_paths

        ############################################################################################

    def collect_training_trajectories(self, collect_policy: BasePolicy, batch_size: int):
        """
        :param collect_policy:  the current policy which we use to collect data
        :param batch_size:  the number of transition steps or trajectories to collect
        :return:
            paths: a list trajectories
            envsteps_this_batch: the sum over the numbers of environment steps in paths
            train_video_paths: paths which also contain videos for visualization purposes
        """
        print("\nCollecting sample trajectories to be used for training...")
        envsteps_this_batch = 0
        paths: List[PathDict] = utils.sample_n_trajectories(
            env=self.env,
            policy=collect_policy,
            agent=self.agent,
            ntrajs=batch_size,
            max_path_length=self.params['ep_len'],
            render=False,
            expert=False
        )

        train_video_paths = None
        if self.log_video:
            print('\nCollecting train rollouts to be used for saving videos...')
            # TODO look in utils and implement sample_n_trajectories -- implemented
            pass

        # TODO: add logging
        if self.log_metrics:
            # TODO:# what should be log in this function
            pass

        return paths, envsteps_this_batch, train_video_paths

    ############################################################################################
    def train_reward(self):
        """
        Algorithm 2: Nonlinear IOC with stochastic gradients 
        """
        print("\nUpdating reward parameters...")
        reward_logs = []
        # K_train_reward_loop = tqdm(range(self.params['num_reward_train_steps_per_iter']),
        #                            desc="reward_update",
        #                            leave=False)
        # 1.
        K_train_reward_loop = range(self.params['num_reward_train_steps_per_iter'])
        for k_rew in K_train_reward_loop:
            # 2. Sample demonstration batch D^_{demo} \subset D_{demo}
            demo_batch = self.agent.sample_rollouts(self.params['train_demo_batch_size'], demo=True)
            # 3. Sample background batch D^_{samp} \subset D_{sample}
            sample_batch = self.agent.sample_recent_rollouts(self.params['train_sample_batch_size'])

            # reshape rollouts' elements to match the dimension in Replay buffer
            for num_rollout, _ in enumerate(demo_batch):
                demo_batch[num_rollout]["log_prob"] = demo_batch[num_rollout]["log_prob"].reshape(-1, 1)
                demo_batch[num_rollout]["reward"] = demo_batch[num_rollout]["reward"].reshape(-1, 1)

            # 4. Append \hat{D}_demo and \hat{D}_samp to background
            self.agent.add_to_buffer(demo_batch, background=True)
            self.agent.add_to_buffer(sample_batch, background=True)

            background_batch = self.agent.sample_background_rollouts(all_rollouts=True)

            # 5,6. Estimate gradient loss and update parameters
            # reward_log = self.agent.train_reward(demo_batch, sample_batch)
            reward_log = self.agent.train_reward(demo_batch, background_batch)
            reward_logs.append(reward_log)

            self.agent.background_buffer.flush()

            # K_train_reward_loop.set_postfix(K_rew=k_rew,
            #                                 reward_loss=reward_log["Training reward loss"],
            #                                 w=self.agent.reward.w.item())
        return reward_logs

    ############################################################################################
    def train_policy(self) -> List[Sequence[Dict[str, np.ndarray]]]:
        """
        Guided policy search or Policy Gradient
        """
        print('\nTraining agent using sampled data from replay buffer...')
        train_policy_logs = []
        # K_train_policy_loop = tqdm(range(self.params['num_policy_train_steps_per_iter']),
        #                            desc="policy_update",
        #                            leave=False)
        K_train_policy_loop = range(self.params['num_policy_train_steps_per_iter'])
        for k in K_train_policy_loop:
            ob_batch, ac_batch, re_batch, next_ob_batch, terminal_batch = self.agent.sample(
                self.params['train_batch_size'],
                demo=False
            )
            policy_loss = self.agent.train_policy(ob_batch, ac_batch, re_batch,
                                                  next_ob_batch, terminal_batch)
            train_policy_logs.append(policy_loss)

            # K_train_policy_loop.set_postfix(K_ply=k,
            #                                 policy_loss=policy_loss["Training Loss"],
            #                                 )
        return train_policy_logs

    ############################################################################################
    def perform_logging(self, itr: int, paths: List[PathDict],
                        eval_policy, train_video_paths: List[PathDict],
                        reward_logs: list, policy_logs: list
                        ) -> None:

        last_log = policy_logs[-1]

        #######################

        # collect eval trajectories, for logging
        print("\nCollecting data for eval...")
        eval_paths, _ = utils.sample_trajectories(
            self.env,
            eval_policy,
            agent=self.agent,
            min_timesteps_per_batch=self.params['eval_batch_size'],
            max_path_length=self.params['ep_len'],
            render=True
        )

        # save eval rollouts as videos in tensorboard event file
        if self.log_video and train_video_paths is not None:
            # TODO: change to use sample_n_trajectories
            # eval_video_paths, _ = utils.sample_trajectories(
            # self.env, eval_policy, self.agent, MAX_NVIDEO, render=True)
            #
            # # save train/eval videos
            # print('\nSaving train and eval rollouts as videos...')
            # self.logger.log_paths_as_videos(train_video_paths, itr, fps=self.fps, max_videos_to_save=MAX_NVIDEO,
            #                                 video_title='train_rollouts')
            # self.logger.log_paths_as_videos(eval_video_paths, itr, fps=self.fps, max_videos_to_save=MAX_NVIDEO,
            #                                 video_title='eval_rollouts')
            pass
        #######################

        # save eval metrics
        # TODO: should parse the reward training loss and policy training loss
        # TODO: should add a visualization tool to check the trained reward function
        if self.log_metrics:
            # returns, for logging
            train_returns = [path["reward"].sum() for path in paths]
            eval_returns = [eval_path["reward"].sum() for eval_path in eval_paths]

            # episode lengths, for logging
            train_ep_lens = [len(path["reward"]) for path in paths]
            eval_ep_lens = [len(eval_path["reward"]) for eval_path in eval_paths]

            # decide what to log
            logs = OrderedDict()
            logs["Eval_AverageReturn"] = np.mean(eval_returns)
            logs["Eval_StdReturn"] = np.std(eval_returns)
            logs["Eval_MaxReturn"] = np.max(eval_returns)
            logs["Eval_MinReturn"] = np.min(eval_returns)
            logs["Eval_AverageEpLen"] = np.mean(eval_ep_lens)

            logs["Train_AverageReturn"] = np.mean(train_returns)
            logs["Train_StdReturn"] = np.std(train_returns)
            logs["Train_MaxReturn"] = np.max(train_returns)
            logs["Train_MinReturn"] = np.min(train_returns)
            logs["Train_AverageEpLen"] = np.mean(train_ep_lens)

            logs["Train_EnvstepsSoFar"] = self.total_envsteps
            logs["TimeSinceStart"] = time.time() - self.start_time
            logs.update(last_log)

            if itr == 0:
                self.initial_return = np.mean(train_returns)
            logs["Initial_DataCollection_AverageReturn"] = self.initial_return

            # perform the logging
            for key, value in logs.items():
                print('{} : {}'.format(key, value))
                self.logger.log_scalar(value, key, itr)
            print('Done logging...\n\n')

            self.logger.flush()

    def buffer_status(self, demo=True, samp=True, background=True) -> None:
        """ Show length and size of buffers"""
        if demo:
            demo_paths_len = len(self.agent.demo_buffer)
            demo_data_len = self.agent.demo_buffer.num_data
            print(f"Demo_buffer_size: {demo_paths_len}, {demo_data_len}"
                  f" Average ep_len: {demo_data_len / demo_paths_len}")
        if samp:
            samp_paths_len = len(self.agent.sample_buffer)
            samp_data_len = self.agent.sample_buffer.num_data
            print(f"Sample_buffer_size: {samp_paths_len}, {samp_data_len}"
                  f" Average ep_len: {samp_data_len / samp_paths_len}")
        if background:
            back_paths_len = len(self.agent.background_buffer)
            back_data_len = self.agent.background_buffer.num_data
        print(f"Back_buffer_size: {len(self.agent.background_buffer)}, {self.agent.background_buffer.num_data}"
              f" Average ep_len: {back_data_len / back_paths_len :.3f} ")
        print("##########################################################################")
