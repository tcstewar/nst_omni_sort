import nstbot
import numpy as np


periods = [2860, 4000, 5000, 6670]
freqs = 1000000 / np.array(periods, dtype=float)

use_bot = True

if not hasattr(nstbot, 'mybot'):
    if use_bot:
        bot = nstbot.OmniArmBot()
        bot.connect(nstbot.SocketList({'motors': ['10.162.177.29', 54322],
                                       'retina_left': ['10.162.177.29', 54320],
                                       'retina_right': ['10.162.177.29', 54321],
                                       'retina_arm': ['10.162.177.29', 54323],}))
        bot.tracker('retina_left', True, tracking_freqs=freqs, streaming_period=10000)
        bot.tracker('retina_right', True, tracking_freqs=freqs, streaming_period=10000)
        bot.tracker('retina_arm', True, tracking_freqs=freqs, streaming_period=10000)
        bot.send('motors', 'init_motors', '!G31610\n!G41170\n!G51276\n!G6210\n')
        bot.send('motors', 'init_motors_speed', '!P520\n')
    else:
        class DummyBot(object):
            def base_pos(self, x, msg_period):
                pass
            def arm(self, x, msg_period):
                pass
            def get_tracker_info(self, name, index):
                v = np.linspace(-0.8, 0.8, len(freqs))[index]
                return v,0,0,1
        bot = DummyBot()
    nstbot.mybot = bot

else:
    bot = nstbot.mybot

import nengo


class Bot(nengo.Network):
    def __init__(self, bot, msg_period=0.3,
                 arm_offset=[np.pi+1, np.pi-1.5, np.pi-1, 1]):
        super(Bot, self).__init__()
        self.arm_offset = np.array(arm_offset)
        with self:
            def bot_base(t, x):
                bot.base_pos(x, msg_period=msg_period)
                return x
            self.base_pos = nengo.Node(bot_base, size_in=3)

            def bot_tracker(t):
                r = []
                for i in range(len(freqs)):
                    r.extend([
                        bot.get_tracker_info('retina_left', i),
                        bot.get_tracker_info('retina_right', i),
                        bot.get_tracker_info('retina_arm', i),
                        ])
                return np.hstack(r)

            self.tracker = nengo.Node(bot_tracker)


            def bot_arm(t, x):
                bot.arm(x+self.arm_offset, msg_period=msg_period)
                return x
            self.arm = nengo.Node(bot_arm, size_in=4)

            self.ctrl_arm = nengo.Node([0,0,0,0])
            nengo.Connection(self.ctrl_arm, self.arm, synapse=None)

            self.ctrl_base = nengo.Node([0,0,0])
            nengo.Connection(self.ctrl_base, self.base_pos, synapse=None)


class OutOfOrder(nengo.Network):
    def __init__(self, botnet):
        super(OutOfOrder, self).__init__()
        with self:
            self.c_input = nengo.Node(None, size_in=len(freqs))
            self.certainty = nengo.Ensemble(n_neurons=100, dimensions=len(freqs),
                                            radius=np.sqrt(len(freqs)),
                                            eval_points=nengo.dists.Uniform(0,1))
            nengo.Connection(self.c_input, self.certainty, synapse=None)

            self.x_input = nengo.Node(None, size_in=len(freqs))

            self.diff = nengo.Ensemble(n_neurons=300, dimensions=len(freqs)-1)
            nengo.Connection(self.x_input[:-1], self.diff, transform=-1)
            nengo.Connection(self.x_input[1:], self.diff, transform=1)

            self.odd = nengo.Ensemble(n_neurons=400, dimensions=len(freqs))
            def detect_odd(x):
                mean = np.mean(x)
                x = x - mean
                score = np.zeros(len(freqs))
                score[:-1] += x
                score[1:] -= x
                score[0] *= 1.75
                score[-1] *= 1.75

                score = np.abs(score)
                worst = np.argmax(score)
                return np.eye(len(freqs))[worst]
            nengo.Connection(self.diff, self.odd, function=detect_odd)

            self.not_see_all = nengo.Ensemble(n_neurons=100, dimensions=1,
                                              encoders=nengo.dists.Choice([[1]]),
                                              intercepts=nengo.dists.Uniform(0.3, 0.9))
            def not_see_all_func(x):
                if min(x)<0.3:
                    return 1
                else:
                    return 0
            nengo.Connection(self.certainty, self.not_see_all, function=not_see_all_func)

            self.evidence = nengo.networks.EnsembleArray(n_neurons=100, n_ensembles=len(freqs),
                                                         encoders=nengo.dists.Choice([[1]]),
                                                         intercepts=nengo.dists.Uniform(0, 1))
            nengo.Connection(self.evidence.output, self.evidence.input, synapse=0.1, transform=1.1)
            nengo.Connection(self.evidence.output, self.evidence.input, synapse=0.1,
                                transform=-0.7*(1-np.eye(len(freqs))))

            nengo.Connection(self.not_see_all, self.odd.neurons,
                                transform=np.ones((self.odd.n_neurons, 1))*-5)

            nengo.Connection(self.odd, self.evidence.input, transform=0.2)


            self.forget = nengo.Node([0])
            for ens in self.evidence.ensembles:
                nengo.Connection(self.forget, ens.neurons,
                                 transform=-5*np.ones((ens.n_neurons, 1)))


        nengo.Connection(botnet.tracker[::12], self.x_input)
        nengo.Connection(botnet.tracker[3::12], self.c_input)


class TargetInfo(nengo.Network):
    def __init__(self, order):
        super(TargetInfo, self).__init__()
        with self:
            self.info_array = nengo.networks.EnsembleArray(n_neurons=100, n_ensembles=12)
            self.info = self.info_array.output

            self.freqs = nengo.networks.EnsembleArray(n_neurons=100, n_ensembles=12*len(freqs))
            for i in range(len(freqs)):
                nengo.Connection(self.freqs.output[i*12:(i+1)*12], self.info_array.input)

            self.inhibit = nengo.networks.EnsembleArray(n_neurons=100, n_ensembles=len(freqs),
                                                         encoders=nengo.dists.Choice([[1]]),
                                                         intercepts=nengo.dists.Uniform(0.3, 0.9))
            self.bias = nengo.Node(1)
            nengo.Connection(self.bias, self.inhibit.input, transform=np.ones((len(freqs), 1)))
            for i in range(len(freqs)):
                for j in range(12):
                    post = self.freqs.ensembles[i*12+j]
                    nengo.Connection(self.inhibit.ensembles[i], post.neurons,
                                     transform=-5*np.ones((post.n_neurons, 1)))



        nengo.Connection(botnet.tracker, self.freqs.input, synapse=None)
        for i in range(len(freqs)):
            post = self.inhibit.ensembles[i]
            nengo.Connection(order.evidence.ensembles[i],
                             post.neurons,
                             transform=-5*np.ones((post.n_neurons, 1)))


class OrientLR(nengo.Network):
    def __init__(self, target, botnet):
        super(OrientLR, self).__init__()
        #self.config[nengo.Ensemble].neuron_type = nengo.Direct()
        with self:
            self.x_data = nengo.Ensemble(n_neurons=500, dimensions=6, radius=3)
        nengo.Connection(target.info[[0,3,4,7,8,11]], self.x_data)

        with self:
            self.x_pos = nengo.Ensemble(n_neurons=200, dimensions=2)
            def compute_pos(x):
                lx, lc, rx, rc, ax, ac = x

                c = (lc + rc + ac)
                if c <= 0.1:
                    return 0
                return (lx*lc+rx*rc+ax*ac) / c

            nengo.Connection(self.x_data, self.x_pos[0], function=compute_pos)

            self.activation = nengo.Node(None, size_in=1)
            nengo.Connection(self.activation, self.x_pos[1], synapse=None)

        nengo.Connection(self.x_pos, botnet.base_pos[2],
                         function=lambda x: x[0]*x[1],
                         transform=-1)


class OrientFB(nengo.Network):
    def __init__(self, target, botnet):
        super(OrientFB, self).__init__()
        #self.config[nengo.Ensemble].neuron_type = nengo.Direct()

        with self:
            self.data = nengo.Ensemble(n_neurons=500, dimensions=2, radius=1.5)
        nengo.Connection(target.info[[0, 4]], self.data)

        with self:
            self.spd = nengo.Ensemble(n_neurons=500, dimensions=2, radius=1.5)
            def dist_func(x):
                mid = x[0]+x[1]
                diff = x[0] - x[1]

                target_separation = 0.8
                if -0.5<mid<0.5:
                    return (target_separation - diff)*10
                else:
                    return 0
            nengo.Connection(self.data, self.spd[0], function=dist_func)


            self.activation = nengo.Node(None, size_in=1)
            nengo.Connection(self.activation, self.spd[1], synapse=None)

        nengo.Connection(self.spd, botnet.base_pos[0],
                         function=lambda x: x[0]*x[1])

class GraspPosition(nengo.Network):
    def __init__(self, botnet):
        super(GraspPosition, self).__init__()
        with self:
            self.activation = nengo.Node(None, size_in=1)
        nengo.Connection(self.activation, botnet.arm,
                transform=[[-1.86], [-0.28], [2.10], [0]])


class ArmOrientLR(nengo.Network):
    def __init__(self, target, botnet, strength=1):
        super(ArmOrientLR, self).__init__()
        #self.config[nengo.Ensemble].neuron_type = nengo.Direct()
        with self:
            self.x_data = nengo.Ensemble(n_neurons=500, dimensions=2, radius=3)
        nengo.Connection(target.info[[8,11]], self.x_data)

        with self:
            self.x_pos = nengo.Ensemble(n_neurons=200, dimensions=2)
            def compute_pos(x):
                x, c = x

                if c <= 0.1:
                    return 0
                return -x

            nengo.Connection(self.x_data, self.x_pos[0], function=compute_pos)

            self.activation = nengo.Node(None, size_in=1)
            nengo.Connection(self.activation, self.x_pos[1], synapse=None)

        nengo.Connection(self.x_pos, botnet.base_pos[2],
                         function=lambda x: x[0]*x[1],
                         transform=strength)


class StayAway(nengo.Network):
    def __init__(self, botnet):
        super(StayAway, self).__init__()
        with self:
            self.activation = nengo.Node(None, size_in=1)
        nengo.Connection(self.activation, botnet.base_pos[0],
                transform=-1.5)

class Grip(nengo.Network):
    def __init__(self, botnet):
        super(Grip, self).__init__()
        with self:
            self.activation = nengo.Node(None, size_in=1)
        nengo.Connection(self.activation, botnet.arm[3],
                transform=-0.4)



class BehaviourControl(nengo.Network):
    def __init__(self, behaviours):
        super(BehaviourControl, self).__init__()
        with self:
            self.behave = nengo.Node([0]*len(behaviours))
        for i, b in enumerate(behaviours):
            nengo.Connection(self.behave[i], b.activation, synapse=None)


class TaskGrab(nengo.Network):
    def __init__(self, target, botnet, behaviours, grabbed):
        super(TaskGrab, self).__init__()
        #self.config[nengo.Ensemble].neuron_type = nengo.Direct()
        with self:
            self.activation = nengo.Node(None, size_in=1)
            self.behave = nengo.networks.EnsembleArray(n_neurons=400,
                                n_ensembles=6, ens_dimensions=2, radius=1.5,
                                )
            self.behave.add_output('scaled', function=lambda x: x[0]*x[1])
            for i in range(len(behaviours)):
                nengo.Connection(self.activation, self.behave.ensembles[i][1],
                                 synapse=None)

            self.everything = nengo.Ensemble(n_neurons=100, dimensions=12,
                                    neuron_type=nengo.Direct())
            def do_it(x):
                lx,ly,lr,lc, rx,ry,rr,rc, ax,ay,ar,ac = x

                ORIENT_LR = 0
                ARM_ORIENT_LR = 1
                ORIENT_FB = 2
                GRASP_POS = 3
                STAY_AWAY = 4
                GRIP = 5

                diff = lx - rx

                result = [0,0,0,0,0,0]
                if (lc < 0.5 or rc < 0.5) and ac < 0.1:
                    result[STAY_AWAY] = 1
                else:
                    result[ORIENT_FB] = 1
                    result[GRASP_POS] = 1
                    if ac > 0.1:
                        result[ARM_ORIENT_LR] = 1
                    else:
                        result[ORIENT_LR] = 1
                        result[STAY_AWAY] = 1
                        if diff > 0.75:   # if we are too close
                            result[GRASP_POS] = 0

                return result
            nengo.Connection(self.everything, self.behave.input[::2],
                             function=do_it)

            self.should_close = nengo.Ensemble(n_neurons=200, dimensions=1,
                                   radius=1.2)
            def do_should_close(x):
                result = 0
                lx,ly,lr,lc, rx,ry,rr,rc, ax,ay,ar,ac = x
                diff = lx - rx
                mid = (lx + rx) / 2
                if lc > 0.5 and rc > 0.5 and np.abs(diff-0.7)<0.03 and np.abs(mid)<0.05:
                    result = 1
                return result
            nengo.Connection(self.everything, self.should_close, function=do_should_close,
                             synapse=None)
            nengo.Connection(self.should_close, self.behave.input[10], synapse=0.2)
        nengo.Connection(self.should_close, grabbed.has_grabbed,
                         synapse=0.1, transform=2)

        nengo.Connection(target.info, self.everything, synapse=None)

        for i, b in enumerate(behaviours):
            nengo.Connection(self.behave.scaled[i], b.activation, synapse=None)



class TaskHold(nengo.Network):
    def __init__(self, grip):
        super(TaskHold, self).__init__()
        with self:
            self.activation = nengo.Node(None, size_in=1)
        nengo.Connection(self.activation, grip.activation, transform=1)



class Grabbed(nengo.Network):
    def __init__(self, botnet):
        super(Grabbed, self).__init__()
        with self:
            self.has_grabbed = nengo.Ensemble(n_neurons=50, dimensions=1,
                                              neuron_type=nengo.LIFRate())
            def state(x):
                if x<0.5: return 0
                else: return 1
            nengo.Connection(self.has_grabbed, self.has_grabbed, synapse=0.1)
        def opened_gripper(x):
            if x > -0.1:
                return -1
            else:
                return 0
        nengo.Connection(botnet.arm[3], self.has_grabbed,
                         function=opened_gripper)

class TaskGrabAndHold(nengo.Network):
    def __init__(self, task_grab, task_hold, grabbed):
        super(TaskGrabAndHold, self).__init__()
        #self.config[nengo.Ensemble].neuron_type = nengo.Direct()

        with self:
            self.activation = nengo.Node(None, size_in=1)

            self.choice = nengo.Ensemble(n_neurons=300, dimensions=2, radius=1.5)
            nengo.Connection(self.activation, self.choice[0])
        nengo.Connection(grabbed.has_grabbed, self.choice[1])
        def choose_grab(x):
            if x[1] < 0.5 and x[0]>0.5:
                return 1
            else:
                return 0
        nengo.Connection(self.choice, task_grab.activation, function=choose_grab)
        def choose_hold(x):
            if x[1] > 0.5 and x[0]>0.5:
                return 1
            else:
                return 0
        nengo.Connection(self.choice, task_hold.activation, function=choose_hold)



model = nengo.Network(seed=2)
model.config[nengo.Ensemble].neuron_type = nengo.LIFRate()
model.config[nengo.Connection].solver = nengo.solvers.LstsqL2(reg=0.1)
with model:
    botnet = Bot(bot)
    order = OutOfOrder(botnet)
    target = TargetInfo(order)
    orient_lr = OrientLR(target, botnet)
    arm_orient_lr = ArmOrientLR(target, botnet)
    orient_fb = OrientFB(target, botnet)
    grasp_pos = GraspPosition(botnet)
    stay_away = StayAway(botnet)
    grip = Grip(botnet)

    grabbed = Grabbed(botnet)

    task_grab = TaskGrab(target, botnet, [orient_lr, arm_orient_lr, orient_fb,
                           grasp_pos, stay_away, grip], grabbed)

    task_hold = TaskHold(grip)

    task_grab_and_hold = TaskGrabAndHold(task_grab, task_hold, grabbed)

    bc = BehaviourControl([orient_lr, arm_orient_lr, orient_fb,
                           grasp_pos, stay_away, grip, task_grab, task_hold,
                           task_grab_and_hold])


if __name__ == '__main__':
    with model:
        start = nengo.Node([1])
        nengo.Connection(start, task_grab_and_hold.activation)
    sim = nengo.Simulator(model)
    while True:
        sim.run(10)
