import torch
from datetime import datetime
import torch.nn.functional as F
import torch.optim as optim
from torch.autograd import Variable
import numpy as np

import helper
from os.path import join as path_join
from gridworld_goals import gameEnv, gameOb
from model import DFP_Network
import imageio
from pycrayon import CrayonClient

cc = CrayonClient(hostname="localhost")


def get_f(m, offsets):
    '''
    takes a time-series of measurements as well as a set of temporal offsets, and produces the 'f' values for those
    measurements, which corresponds to how they change in the future at each offset
    :param m: time-series of measurements
    :param offsets: list of temporal offset (how many step in the future we have to predict)
    :return: prediction of how the measurements change in the future for each offset -> f = <m_T1  – m_0,m_T2  – m_0, …, m_Tn  – m_0>
    '''
    f = np.zeros([len(m), m.shape[1], len(offsets)])
    for i, offset in enumerate(offsets):
        f[:-offset, :, i] = m[offset:, :] - m[:-offset, :]
        if i > 0:
            f[-offset:, :, i] = f[-offset:, :, i-1]
    return f

def ensure_shared_grads(model, shared_model):
    '''
    Ensure that the gradient is shared among the master and the slayer
    :param model: neural networks trained
    :param shared_model: main copy of the model to update
    :return:
    '''
    for param, shared_param in zip(model.parameters(),
                                   shared_model.parameters()):
        if shared_param.grad is not None:
            return
        shared_param._grad = param.grad

def train(episode_buffer, exp_buffer, local_net, master_net, offsets, optimizer, max_grad_norm):
    '''
    execute one optimizing step based on the experience accumulated
    :param episode_buffer: episode history
    :param exp_buffer: expierence history
    :param local_net: local network used to predict the measurements
    :param master_net: master network used to update the gradient
    :param offsets: offsets used during temporal difference prediction
    :param optimizer: optimizer used
    :param max_grad_norm: max value of the gradient
    :return: loss and entropy
    '''
    episode_buffer = np.array(episode_buffer)
    measurements = np.vstack(episode_buffer[:, 2])
    targets = get_f(measurements, offsets)               # Generate targets using measurements and offsets
    episode_buffer[:, local_net.action_size] = zip(targets)            # Teach to the network to predict the change in the measurements
    exp_buffer.add(zip(episode_buffer))

    # Get a batch of experiences from the buffer and use them to update the global network
    if len(exp_buffer.buffer) > local_net.batch_size:
        exp_batch = exp_buffer.sample(local_net.batch_size)
        obeservation_ = np.stack(exp_batch[:, 0], axis=0)
        measurements_ = np.vstack(exp_batch[:, 2])
        temperature_ = [0.1]
        action_ = exp_batch[:, 1]
        target_ = np.vstack(exp_batch[:, 4])
        goal_ = np.vstack(exp_batch[:, 3])
        local_net.forward(obeservation_, measurements_, goal_, temperature_)
        loss, entropy = local_net.loss(action_, target_)

        optimizer.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm(local_net.parameters(), max_grad_norm)

        ensure_shared_grads(local_net, master_net)
        optimizer.step()
        return loss, entropy
    else:
        return 0, 0


def work(rank, args, master_net, exp_buffer, optimizer=None):
    torch.manual_seed(args.seed + rank)

    episodes = master_net.episodes
    episode_deliveries = []
    episode_lengths = []
    episode_mean_values = []

    # Create the local copy of the network
    env = gameEnv(args.partial, args.env_size, args.action_space)
    local_net = DFP_Network(args.batch_szie,
                            num_offsets=args.num_offset,
                            a_size=args.action_space,
                            num_measurements=args.num_measurements)
    assert args.num_measurements == len(env.measurements)



    if optimizer is None:
        optimizer = optim.Adam(master_net.parameters(), lr=args.learning_rate)

    print("Starting work on worker-{}".format(rank))

    while not master_net.shoud_stop():
        local_net.load_state_dict(master_net.local_net.state_dict())         # Copy parameters from global to local network
        episode_buffer = []
        episode_frames = []
        done = False
        step = 0
        temp = 0.25  # How spread out we want our action distribution to be

        observation, o_big, measurements, delivery_pos, drone_pos = env.reset()
        the_measurements = measurements
        while not done:

            # Here is where our goal-switching takes place
            # When the battery charge is below 0.3, we set the goal to optimize battery
            # When the charge is above that value we set the goal to optimize deliveries
            if measurements[1] <= .3:
                goal = np.array([0.0, 1.0])
            else:
                goal = np.array([1.0, 0.0])

            action_dist = local_net.forward(observation, measurements, goal, temp)

            b = goal * torch.transpose(action_dist, 0, 1)
            c = np.sum(b, 1)
            c /= c.sum()

            # Choose greedy action
            action = np.random.choice(c, p=c)
            action = np.argmax(c == action)

            observation_new, o_new_big, measurements_new, delivery_pos_new, drone_pos_new, done = env.step(action)
            episode_buffer.append([observation, action, np.array(measurements), goal,
                                   np.zeros(len(args.num_offsets))])

            if rank == 0 and master_net.episodes % 150 == 0:
                episode_frames.append(helper.set_image_gridworld(o_new_big, measurements_new, step + 1,
                                                                 delivery_pos_new, drone_pos_new))

            observation = np.copy(observation_new)
            measurements = measurements_new[:]
            delivery_pos = delivery_pos_new[:]
            drone_pos = drone_pos_new
            step += 1

            # End the episode after 100 steps
            if step > 100:
                done = True

        episode_deliveries.append(measurements[0])
        episode_lengths.append(step)

        # Update the network using the experience buffer at the end of the episode.
        if train:
            loss, entropy = train(episode_buffer, exp_buffer,
                                  local_net=local_net,
                                  master_net=master_net,
                                  offsets=args.offsets,
                                  optimizer=optimizer,
                                  max_grad_norm=args.max_grad_norm)

        # Periodically save gifs of episodes, model parameters, and summary statistics.
        if episodes % 50 == 0 and episodes != 0:
            if episodes % 2000 == 0 and rank == 0 and train:
                model_file = path_join(args.model_path, 'model-{}.cptk'.format(episodes))
                torch.save(master_net.state_dict(), helper.ensure_dir(model_file))
                print("Saved Model")

            if rank == 0 and episodes % 150 == 0:
                time_per_step = 0.25
                images = np.array(episode_frames)
                image_file = path_join(args.gif_path + '/image-{}.gif'.format(episodes))
                imageio.mimsave(helper.ensure_dir(image_file), images, duration=time_per_step)

            mean_deliveries = np.mean(episode_deliveries[-50:])
            mean_length = np.mean(episode_lengths[-50:])
            mean_value = np.mean(episode_mean_values[-50:])

            summary_file = path_join(args.model_path, "exp-{}".format(datetime.now()))
            summary = cc.get_experiment_names(helper.ensure_dir(summary_file))
            summary.add_scalar_value('Performance/Deliveries', float(mean_deliveries))
            summary.add_scalar_value('Performance/Length', float(mean_length))
            summary.add_scalar_value('Performance/Mean', float(mean_value))

            if train == True:
                summary.add_scalar_value('Losses/Loss', simple_value=float(loss))
                summary.add_scalar_value('Losses/Entory', simple_value=float(entropy))

        episodes += 1
        master_net.episodes += 1

