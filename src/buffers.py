import torch
# from torch import nn
# import kornia
import numpy as np
import gym
import copy
import psutil

# from utils import random_crop


class ReplayBuffer:
    """Buffer to store environment transitions

    (Chongyi Zheng): update replay buffer to stable_baselines style to save memory

    Reference:
    - https://github.com/hill-a/stable-baselines/blob/master/stable_baselines/common/buffers.py

    """
    def __init__(self, obs_space, action_space, transition_num, device, n_envs=1,
                 optimize_memory_usage=False, handle_timeout_termination=False):

        # assert n_envs == 1, "Replay buffer only support single environment for now"

        self.obs_space = obs_space
        self.action_space = action_space
        self.capacity = transition_num // n_envs
        self.n_envs = n_envs
        self.device = device
        self.optimize_memory_usage = optimize_memory_usage
        self.handle_timeout_termination = handle_timeout_termination

        # Check that the replay buffer can fit into the memory
        if psutil is not None:
            mem_available = psutil.virtual_memory().available

        # the proprioceptive obs is stored as float32, pixels obs as uint8
        obs_shape = obs_space.shape
        action_shape = action_space.shape

        self.obses = np.empty((self.capacity, n_envs, *obs_shape), dtype=np.float32)
        if self.optimize_memory_usage:
            # `observations` contains also the next observation
            self.next_obses = None
        else:
            self.next_obses = np.empty((self.capacity, n_envs, *obs_shape), dtype=np.float32)
        if isinstance(action_space, gym.spaces.Discrete):
            self.actions = np.empty((self.capacity, n_envs, 1), dtype=np.int32)
        elif isinstance(action_space, gym.spaces.Box):
            self.actions = np.empty((self.capacity, n_envs, *action_shape), dtype=np.float32)
        else:
            raise TypeError(f"Unknown action space type: {type(action_space)}")
        self.rewards = np.empty((self.capacity, n_envs, 1), dtype=np.float32)
        self.not_dones = np.empty((self.capacity, n_envs, 1), dtype=np.float32)
        self.timeouts = np.zeros((self.capacity, n_envs, 1), dtype=np.float32)

        if psutil is not None:
            total_memory_usage = self.obses.nbytes + self.actions.nbytes + self.rewards.nbytes + self.not_dones.nbytes
            if self.next_obses is not None:
                total_memory_usage += self.next_obses.nbytes

            if total_memory_usage > mem_available:
                # Convert to GB
                total_memory_usage /= 1e9
                mem_available /= 1e9
                print(
                    "This system does not have apparently enough memory to store the complete "
                    f"replay buffer {total_memory_usage:.2f}GB > {mem_available:.2f}GB"
                )

        self.idx = 0
        self.full = False

    def __len__(self):
        return self.capacity * self.n_envs if self.full else self.idx * self.n_envs

    def reset(self):
        self.idx = 0
        self.full = False

    def add(self, obs, action, reward, next_obs, done, infos):
        np.copyto(self.obses[self.idx], obs)
        np.copyto(self.actions[self.idx], action)
        np.copyto(self.rewards[self.idx], reward.reshape([-1, 1]))
        if self.optimize_memory_usage:
            np.copyto(self.obses[(self.idx + 1) % self.capacity], next_obs)
        else:
            np.copyto(self.next_obses[self.idx], next_obs)
        not_done = np.array([not done_ for done_ in done])
        np.copyto(self.not_dones[self.idx], not_done.reshape([-1, 1]))

        if self.handle_timeout_termination:
            self.timeouts[self.idx] = np.array([info.get("TimeLimit.truncated", False) for info in infos])

        self.idx = (self.idx + 1) % self.capacity
        self.full = self.full or self.idx == 0

    def sample(self, batch_size):
        if not self.optimize_memory_usage:
            idxs = np.random.randint(
                0, self.capacity if self.full else self.idx, size=batch_size // self.n_envs
            )

            next_obses = torch.as_tensor(
                self.next_obses[idxs].reshape([-1, *self.obs_space.shape]),
                device=self.device).float()
        else:
            if self.full:
                idxs = (np.random.randint(1, self.capacity, size=batch_size // self.n_envs)
                        + self.idx) % self.capacity
            else:
                idxs = np.random.randint(0, self.idx, size=batch_size // self.n_envs)

            next_obses = torch.as_tensor(
                self.obses[(idxs + 1) % self.capacity].reshape([-1, *self.obs_space.shape]),
                device=self.device).float()

        obses = torch.as_tensor(self.obses[idxs].reshape([-1, *self.obs_space.shape]),
                                device=self.device)
        actions = torch.as_tensor(self.actions[idxs].reshape([-1, *self.action_space.shape]),
                                  device=self.device)
        rewards = torch.as_tensor(self.rewards[idxs].reshape([-1, 1]), device=self.device)
        if self.handle_timeout_termination:
            # Only use dones that are not due to timeouts
            # deactivated by default (timeouts is initialized as an array of False)
            not_dones = torch.as_tensor(
                np.logical_or(self.not_dones[idxs], self.timeouts[idxs]).astype(
                    self.not_dones.dtype).reshape([-1, 1]),
                device=self.device
            )
        else:
            not_dones = torch.as_tensor(self.not_dones[idxs].reshape([-1, 1]),
                                        device=self.device)

        return obses, actions, rewards, next_obses, not_dones

    # def sample_curl(self, batch_size):
    #     # TODO (chongyi zheng): update this function to drq style
    #     # idxs = np.random.randint(
    #     #     0, self.capacity if self.full else self.idx, size=batch_size
    #     # )
    #     #
    #     # obses = torch.as_tensor(self.obses[idxs], device=self.device).float()
    #     # actions = torch.as_tensor(self.actions[idxs], device=self.device)
    #     # rewards = torch.as_tensor(self.rewards[idxs], device=self.device)
    #     # next_obses = torch.as_tensor(self.next_obses[idxs], device=self.device).float()
    #     # not_dones = torch.as_tensor(self.not_dones[idxs], device=self.device)
    #     # (chongyi zheng): internal sample function
    #     obses, actions, rewards, next_obses, not_dones = self.sample(batch_size)
    #
    #     pos = obses.clone()
    #
    #     # TODO (chongyi zheng): We don't need to crop image as PAD
    #     # obses = random_crop(obses)
    #     # next_obses = random_crop(next_obses)
    #     # pos = random_crop(pos)
    #
    #     curl_kwargs = dict(obs_anchor=obses, obs_pos=pos,
    #                        time_anchor=None, time_pos=None)
    #
    #     return obses, actions, rewards, next_obses, not_dones, curl_kwargs
    #
    # def sample_ensembles(self, batch_size, num_ensembles=1):
    #     ensem_obses, ensem_actions, ensem_rewards, ensem_next_obses, ensem_not_dones = \
    #         self.sample(batch_size * num_ensembles)
    #
    #     # TODO (chongyi zheng): do we need to clone here?
    #     obses = ensem_obses[:batch_size].detach().clone()
    #     actions = ensem_actions[:batch_size].detach().clone()
    #     rewards = ensem_rewards[:batch_size].detach().clone()
    #     next_obses = ensem_next_obses[:batch_size].detach().clone()
    #     not_dones = ensem_not_dones[:batch_size].detach().clone()
    #
    #     ensem_kwargs = dict(obses=ensem_obses, next_obses=ensem_next_obses, actions=ensem_actions)
    #
    #     return obses, actions, rewards, next_obses, not_dones, ensem_kwargs


# class AugmentReplayBuffer(ReplayBuffer):
#     def __init__(self, obs_shape, action_shape, capacity, image_pad, device):
#         super().__init__(obs_shape, action_shape, capacity, device)
#         self.image_pad = image_pad
#
#         self.aug_trans = nn.Sequential(
#             nn.ReplicationPad2d(self.image_pad),
#             kornia.augmentation.RandomCrop((obs_shape[-1], obs_shape[-1])))
#
#     def sample(self, batch_size):
#         obses, actions, rewards, next_obses, not_dones = super().sample(batch_size)
#         obses_aug = obses.detach().clone()
#         next_obses_aug = obses.detach().clone()
#
#         obses = self.aug_trans(obses)
#         next_obses = self.aug_trans(next_obses)
#
#         obses_aug = self.aug_trans(obses_aug)
#         next_obses_aug = self.aug_trans(next_obses_aug)
#
#         return obses, actions, rewards, next_obses, not_dones, obses_aug, next_obses_aug
#
#     def sample_curl(self, batch_size):
#         # TODO (chongyi zheng)
#         pass
#
#     def sample_ensembles(self, batch_size, num_ensembles=1):
#         ensem_obses, ensem_actions, ensem_rewards, ensem_next_obses, ensem_not_dones, \
#         ensem_obses_aug, ensem_next_obses_aug = self.sample(batch_size * num_ensembles)
#
#         # TODO (chongyi zheng): do we need to clone here?
#         obses = ensem_obses[:batch_size].detach().clone()
#         next_obses = ensem_next_obses[:batch_size].detach().clone()
#         obses_aug = ensem_obses[:batch_size].detach.clone()
#         next_obses_aug = ensem_next_obses[:batch_size].detach().clone()
#         actions = ensem_actions[:batch_size].detach().clone()
#         rewards = ensem_rewards[:batch_size].detach().clone()
#         not_dones = ensem_not_dones[:batch_size].detach().clone()
#
#         ensem_kwargs = dict(obses=ensem_obses, next_obses=ensem_next_obses,
#                             obses_aug=ensem_obses_aug, next_obses_aug=ensem_next_obses_aug,
#                             actions=ensem_actions)
#
#         return obses, actions, rewards, next_obses, not_dones, obses_aug, next_obses_aug, ensem_kwargs


class FrameStackReplayBuffer(ReplayBuffer):
    # TODO (chongyi zheng): FrameStackReplayBuffer has bugs
    """Only store unique frames to save memory

    Base on https://github.com/thu-ml/tianshou/blob/master/tianshou/data/buffer/base.py
    """
    def __init__(self, obs_space, action_space, capacity, frame_stack, device, optimize_memory_usage=False):
        single_frame_obs_space = copy.deepcopy(obs_space)
        if single_frame_obs_space.shape[0] != 1:
            # convert to single frame
            obs_shape = list(single_frame_obs_space.shape)
            obs_shape[0] = 1
            single_frame_obs_space.shape = tuple(obs_shape)

        super().__init__(single_frame_obs_space, action_space, capacity, device,
                         optimize_memory_usage=optimize_memory_usage)
        self.frame_stack = frame_stack

        # self.stack_frame_dists = np.empty((capacity, self.frame_stack), dtype=np.int32)

        # (chongyi zheng): We need to set the final not_done = 0.0 to make sure the correct stack when the first
        #   observation is sampled. Note that empty array is initialized to be all zeros.
        # self.not_dones[-1] = 0.0

    def prev(self, index):
        """Return the index of previous transition.
        The index won't be modified if it is the beginning of an episode.
        """
        index = (index - 1) % self.capacity
        end_flag = np.logical_not(self.not_dones[index]).squeeze(-1) | (index == self.idx)
        return (index + end_flag) % self.capacity

    def add(self, obs, action, reward, next_obs, done):
        np.copyto(self.obses[self.idx], obs[-1])
        np.copyto(self.actions[self.idx], action)
        np.copyto(self.rewards[self.idx], reward)
        if self.optimize_memory_usage:
            np.copyto(self.obses[(self.idx + 1) % self.capacity], next_obs[-1])
        else:
            np.copyto(self.next_obses[self.idx], next_obs[-1])
        np.copyto(self.not_dones[self.idx], not done)

        self.idx = (self.idx + 1) % self.capacity
        self.full = self.full or self.idx == 0

    def sample(self, batch_size):
        if not self.optimize_memory_usage:
            prev_idxs = idxs = np.random.randint(
                0, self.capacity if self.full else self.idx, size=batch_size
            )

            obses = []
            next_obses = []
            for _ in range(self.frame_stack):
                obses.append(self.obses[prev_idxs])
                next_obses.append(self.next_obses[prev_idxs])
                prev_idxs = self.prev(prev_idxs)
            obses = np.concatenate(obses, axis=1)
            next_obses = np.concatenate(next_obses, axis=1)

            obses = torch.as_tensor(obses, device=self.device).float()
            next_obses = torch.as_tensor(next_obses, device=self.device).float()
        else:
            if self.full:
                prev_idxs = idxs = (np.random.randint(1, self.capacity, size=batch_size) + self.idx) % self.capacity
            else:
                prev_idxs = idxs = np.random.randint(0, self.idx, size=batch_size)

            obses = []
            next_obses = []
            for _ in range(self.frame_stack):
                obses.append(self.obses[prev_idxs])
                next_obses.append(self.obses[(prev_idxs + 1) % self.capacity])
                prev_idxs = self.prev(prev_idxs)
            obses = np.concatenate(obses, axis=1)
            next_obses = np.concatenate(next_obses, axis=1)

            obses = torch.as_tensor(obses, device=self.device).float()
            next_obses = torch.as_tensor(next_obses, device=self.device).float()

        actions = torch.as_tensor(self.actions[idxs], device=self.device).float()
        rewards = torch.as_tensor(self.rewards[idxs], device=self.device)
        not_dones = torch.as_tensor(self.not_dones[idxs], device=self.device)

        # TODO (chongyi zheng): We don't need to crop image as PAD
        # obses = random_crop(obses)
        # next_obses = random_crop(next_obses)

        return obses, actions, rewards, next_obses, not_dones


# class AugmentFrameStackReplayBuffer(FrameStackReplayBuffer):
#     def __init__(self, obs_shape, action_shape, capacity, frame_stack, image_pad, device, optimize_memory_usage=False):
#         super().__init__(obs_shape, action_shape, capacity, frame_stack, device, optimize_memory_usage)
#         self.image_pad = image_pad
#
#         self.aug_trans = nn.Sequential(
#             nn.ReplicationPad2d(self.image_pad),
#             kornia.augmentation.RandomCrop((obs_shape[-1], obs_shape[-1])))
#
#     def sample(self, batch_size):
#         obses, actions, rewards, next_obses, not_dones = super().sample(batch_size)
#         obses_aug = obses.detach().clone()
#         next_obses_aug = next_obses.detach().clone()
#
#         # TODO (chongyi zheng): We don't need to crop image as PAD
#         # obses = random_crop(obses)
#         # next_obses = random_crop(next_obses)
#
#         obses = self.aug_trans(obses)
#         next_obses = self.aug_trans(next_obses)
#
#         obses_aug = self.aug_trans(obses_aug)
#         next_obses_aug = self.aug_trans(next_obses_aug)
#
#         return obses, actions, rewards, next_obses, not_dones, obses_aug, next_obses_aug
#
#     def sample_ensembles(self, batch_size, num_ensembles=1):
#         # TODO (chongyi zheng)
#         pass
