from _asyncio import Future
import asyncio
from asyncio.queues import Queue
import uvloop
asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

from profilehooks import profile
import logging

import sys
import time
import numpy as np
from numpy.random import dirichlet
from collections import namedtuple
import logging
import daiquiri

daiquiri.setup(level=logging.DEBUG)
logger = daiquiri.getLogger(__name__)

import utils.go as go
from utils.features import extract_features,bulk_extract_features

# All terminology here (Q, U, N, p_UCT) uses the same notation as in the
# AlphaGo paper.
# Exploration constant
c_PUCT = 5
QueueItem = namedtuple("QueueItem", "feature future")

class MCTSPlayerMixin(object):


    now_expanding = set()
    # queue size should be >= the number of semmphores
    # in order to maxout the coroutines
    # There is not rule of thumbs to choose optimal semmphores
    # And keep in mind: the more coroutines, the less (?) quality (?)
    # of the Monte Carlo Tree obtains. As my searching is less deep
    # w.r.t a sequential MCTS. However, since MCTS is a randomnized
    # algorithm that tries to approximate a value by averaging over run_many
    # random processes, the quality of the search tree is hard to define.
    # It's a trade off among time, accuracy, and the frequency of NN updates.
    sem = asyncio.Semaphore(64)
    queue = Queue(64)
    loop = asyncio.get_event_loop()
    running_simulation_num = 0

    __slot__ = ["parent","move","prior","position","children","U",
                "N","W"]

    def __init__(self, parent, move, prior):
        self.parent = parent # pointer to another MCTSNode
        self.move = move # the move that led to this node
        self.prior = prior
        self.position = None # lazily computed upon expansion
        self.children = {} # map of moves to resulting MCTSNode
        self.U,self.N,self.W = 0,0,0

    def __repr__(self):
        return f"<MCTSNode move=self.move prior=self.prior score=self.action_score is_expanded=self.is_expanded()>"

    @property
    def Q(self):
        return self.W/self.N if self.N != 0 else 0

    @property
    def action_score(self):
        return self.Q + self.U

    def virtual_loss_do(self):
        self.N += 3
        self.W -= 3

    def virtual_loss_undo(self):
        self.N -= 3
        self.W += 3

    def is_expanded(self):
        return self.position is not None

    #@profile
    def compute_position(self):
        """Evolve the game board, and return current position"""
        position = self.parent.position.play_move(self.move)
        self.position = position
        return position

    #@profile
    def expand(self, move_probabilities, noise=True):
        """Expand leaf node"""
        if noise:
            move_probabilities = move_probabilities*.75 + 0.25*dirichlet([0.03]*362)

        self.children = {move: MCTSPlayerMixin(self,move,prob)
            for move, prob in np.ndenumerate(np.reshape(move_probabilities[:-1],(go.N,go.N)))}
        # Pass should always be an option! Say, for example, seki.
        self.children[None] = MCTSPlayerMixin(self,None,move_probabilities[-1])

    def backup_value_single(self,value):
        """Backup value of a single tree node"""
        self.N += 1
        if self.parent is None:

            # No point in updating Q / U values for root, since they are
            # used to decide between children nodes.
            return

        # This incrementally calculates node.Q = average(Q of children),
        # given the newest Q value and the previous average of N-1 values.
        self.W, self.U = (
            self.W + value,
            c_PUCT * np.sqrt(self.parent.N) * self.prior / self.N,
        )

    async def start_tree_search(self):

        #TODO: add proper game over condition
        now_expanding = self.__class__.now_expanding

        while self in now_expanding:
            await asyncio.sleep(1e-4)

        if not self.is_expanded(): #  is leaf node

            # add leaf node to expanding list
            now_expanding.add(self)

            # compute leaf node position
            pos = self.compute_position()

            if pos is None:
                #logger.debug("illegal move!")
                # See go.Position.play_move for notes on detecting legality
                # In Go, illegal move means loss (or resign)
                now_expanding.remove(self)
                # must invert, because alternative layer has opposite objective
                return -1*-1

            """Show thinking history for fun"""
            #logger.debug(f"Investigating following position:\n{self.position}")

            # perform dihedral manipuation
            flip_axis,num_rot = np.random.randint(2),np.random.randint(4)
            dihedral_features = extract_features(pos,dihedral=(flip_axis,num_rot))

            # push extracted dihedral features of leaf node to the evaluation queue
            future = await self.__class__.push_queue(dihedral_features)  # type: Future
            await future
            move_probs, value = future.result()

            # perform reversed dihedral maniputation to move_prob
            move_probs = np.append(np.reshape(np.flip(np.rot90(np.reshape(\
            move_probs[:-1],(go.N,go.N)),4-num_rot),axis=flip_axis),(go.N**2,)),move_probs[-1])

            # expand by move probabilities
            self.expand(move_probs)

            # remove leaf node from expanding list
            now_expanding.remove(self)

            # must invert, because alternative layer has opposite objective
            return value[0]*-1

        else: # not a leaf node

            self.virtual_loss_do()

            # select the child node with maximum action acore
            child = max(self.children.values(), key=lambda node: node.action_score)
            # add virtual loss
            child.virtual_loss_do()
            # start child tree search
            value = await child.start_tree_search()
            # subtract virtual loss
            child.virtual_loss_undo()
            # back up child node
            # must invert, because alternative layer has opposite objective
            child.backup_value_single(value*-1)

            # subtract virtual loss imposed at the beginning
            #if self.parent is not None:
            self.virtual_loss_undo()
            # back up value just for current node
            self.backup_value_single(value)

            # must invert
            return value*-1

    @classmethod
    def set_network_api(cls, net):
        cls.api = net

    @classmethod
    def run_many(cls,bulk_features):
        return cls.api.run_many(bulk_features)
        """simulate I/O & evaluate"""
        #sleep(np.random.random()*5e-2)
        #return np.random.random((len(bulk_features),362)), np.random.random((len(bulk_features),1))

    @classmethod
    def set_root_node(self, root: object):
        self.ROOT = root
        self.ROOT.parent = None

    @classmethod
    def move_prob(cls):
        prob = np.asarray([child.N for child in cls.ROOT.children.values()]) / cls.ROOT.N
        prob /= np.sum(prob) # ensure 1.
        return prob

    @classmethod
    def suggest_move_prob(cls, position, iters=1600):
        """Async tree search controller"""
        start = time.time()

        if cls.ROOT.parent is None:
            move_probs,_ = cls.api.run_many(bulk_extract_features([position]))
            cls.ROOT.position = position
            cls.ROOT.expand(move_probs[0])

        coroutine_list = []
        for _ in range(iters):
            coroutine_list.append(cls.tree_search())
        coroutine_list.append(cls.prediction_worker())
        cls.loop.run_until_complete(asyncio.gather(*coroutine_list))

        logger.debug(f"Searched for {(time.time() - start):.5f} seconds")
        return cls.move_prob()

    @classmethod
    async def tree_search(cls):
        """Asynchrounous tree search with semaphores"""

        cls.running_simulation_num += 1

        # reduce parallel search number
        with await cls.sem:

            value = await cls.ROOT.start_tree_search()
            #logger.debug(f"value: {value}")
            #logger.debug(f'Current running threads : {running_simulation_num}')
            cls.running_simulation_num -= 1

            return value

    @classmethod
    async def prediction_worker(cls):
        """For better performance, queueing prediction requests and predict together in this worker.
        speed up about 45sec -> 15sec for example.
        """
        q = cls.queue
        margin = 10  # avoid finishing before other searches starting.
        while cls.running_simulation_num> 0 or margin > 0:
            if q.empty():
                if margin > 0:
                    margin -= 1
                await asyncio.sleep(1e-3)
                continue
            item_list = [q.get_nowait() for _ in range(q.qsize())]  # type: list[QueueItem]
            #logger.debug(f"predicting {len(item_list)} items")
            bulk_features = np.asarray([item.feature for item in item_list])
            policy_ary, value_ary = cls.run_many(bulk_features)
            for p, v, item in zip(policy_ary, value_ary, item_list):
                item.future.set_result((p, v))

    @classmethod
    async def push_queue(cls, features):
        future = cls.loop.create_future()
        item = QueueItem(features, future)
        await cls.queue.put(item)
        return future