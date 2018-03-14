import json
import logging
import multiprocessing as mp
from threading import Thread

import requests
from crankycoin.repository import Blockchain, Mempool, Peers
from crankycoin.models import BlockHeader, Transaction
from crankycoin.services import Validator
from crankycoin import config, logger, app


class NodeMixin(object):

    FULL_NODE_PORT = config['network']['full_node_port']
    NODES_URL = config['network']['nodes_url']
    INBOX_URL = config['network']['inbox_url']
    TRANSACTIONS_URL = config['network']['transactions_url']
    TRANSACTIONS_INV_URL = config['network']['transactions_inv_url']
    BLOCKS_INV_URL = config['network']['blocks_inv_url']
    BLOCKS_URL = config['network']['blocks_url']
    TRANSACTION_HISTORY_URL = config['network']['transaction_history_url']
    BALANCE_URL = config['network']['balance_url']
    DOWNTIME_THRESHOLD = config['network']['downtime_threshold']
    STATUS_URL = config['network']['status_url']
    CONNECT_URL = config['network']['connect_url']
    MIN_PEERS = config['network']['min_peers']
    MAX_PEERS = config['network']['max_peers']

    def __init__(self):
        self.peers = Peers()
        self.check_peers()

    def request_nodes(self, node, port):
        url = self.NODES_URL.format(node, port)
        try:
            response = requests.get(url)
            if response.status_code == 200:
                all_nodes = json.loads(response.json())
                return all_nodes
        except requests.exceptions.RequestException as re:
            self.peers.record_downtime(node)
        return None

    def find_known_peers(self):
        peers = self.peers.get_all_peers()
        known_peers = peers.copy()
        for peer in peers:
            nodes = self.request_nodes(peer, self.FULL_NODE_PORT)
            if nodes is not None:
                known_peers = known_peers.union(nodes["full_nodes"])
        return known_peers

    def ping_status(self, host):
        url = self.STATUS_URL.format(host, self.FULL_NODE_PORT)
        try:
            response = requests.get(url)
            if response.status_code == 200:
                status_dict = json.loads(response.json())
                return status_dict == config['network']
        except requests.exceptions.RequestException as re:
            pass
        return None

    def broadcast_transaction(self, transaction):
        # Used only when broadcasting a transaction that originated locally
        self.check_peers()
        data = {
            "transaction": transaction.to_dict()
        }
        for node in self.peers.get_all_peers():
            url = self.TRANSACTIONS_URL.format(node, self.FULL_NODE_PORT, "")
            try:
                response = requests.post(url, json=data)
                return response
            except requests.exceptions.RequestException as re:
                self.peers.record_downtime(node)
        return None
        # TODO: convert to grequests and return list of responses

    def check_peers(self):
        raise NotImplementedError


class FullNode(NodeMixin):
    NODE_TYPE = "full"
    blockchain = None

    def __init__(self, host, reward_address, **kwargs):
        mp.log_to_stderr()
        mp_logger = mp.get_logger()
        mp_logger.setLevel(logging.DEBUG)
        self.host = host
        self.queue = mp.Queue()
        self.reward_address = reward_address
        self.blockchain = Blockchain()
        self.mempool = Mempool()
        self.validation = Validator(self.blockchain, self.mempool)

        logger.debug("full node server starting on %s with reward address of %s...", host, reward_address)
        self.bottle_thread = Thread(target=app.run, kwargs=dict(host=host, port=self.FULL_NODE_PORT))
        self.bottle_thread.start()
        logger.debug("full node server started on %s with reward address of %s...", host, reward_address)
        super(FullNode, self).__init__()
        mining = kwargs.get("mining")
        if mining is True:
            self.NODE_TYPE = "miner"
            self.exit_flag = False
            self.mining_thread = Thread(target=self.mine)
            self.mining_thread.start()
            logger.debug("mining node started on %s with reward address of %s...", host, reward_address)

    def shutdown(self):
        if self.NODE_TYPE == "miner":
            self.exit_flag = True
            self.mining_thread.join()
        self.bottle_thread.join()

    def dequeue(self, queue):
        while True:
            msg = queue.get()
            if msg == 'SIG_EXIT':
                break
            return msg

    def check_peers(self):
        if self.peers.get_peers_count() < self.MIN_PEERS:
            known_peers = self.find_known_peers()
            host_data = {
                "host": self.host
            }

            for peer in known_peers:
                if self.peers.get_peers_count() >= self.MAX_PEERS:
                    break
                if peer == self.host:
                    continue

                status_url = self.STATUS_URL.format(peer, self.FULL_NODE_PORT)
                connect_url = self.CONNECT_URL.format(peer, self.FULL_NODE_PORT)
                try:
                    response = requests.get(status_url)
                    if response.status_code != 200 or json.loads(response.json()) != config['network']:
                        continue
                    response = requests.post(connect_url, json=host_data)
                    if response.status_code == 202 and json.loads(response.json()).get("success") is True:
                        self.peers.add_peer(peer)
                except requests.exceptions.RequestException as re:
                    pass
        return

    def request_block_header(self, node, port, block_hash=None, height=None):
        if block_hash is not None:
            url = self.BLOCKS_URL.format(node, port, "hash", block_hash)
        elif height is not None:
            url = self.BLOCKS_URL.format(node, port, "height", height)
        else:
            url = self.BLOCKS_URL.format(node, port, "height", "latest")
        try:
            response = requests.get(url)
            if response.status_code == 200:
                block_dict = json.loads(response.json())
                block_header = BlockHeader(
                    block_dict['previous_hash'],
                    block_dict['merkle_root'],
                    block_dict['timestamp'],
                    block_dict['nonce'],
                    block_dict['version'])
                return block_header
        except requests.exceptions.RequestException as re:
            logger.warn("Request Exception with host: {}".format(node))
            self.peers.record_downtime(node)
        return None

    def request_transaction(self, node, port, tx_hash):
        url = self.TRANSACTIONS_URL.format(node, port, tx_hash)
        try:
            response = requests.get(url)
            if response.status_code == 200:
                tx_dict = json.loads(response.json())
                transaction = Transaction(
                    tx_dict['source'],
                    tx_dict['destination'],
                    tx_dict['amount'],
                    tx_dict['fee'],
                    tx_dict['prev_hash'],
                    tx_dict['tx_type'],
                    tx_dict['timestamp'],
                    tx_dict['tx_hash'],
                    tx_dict['asset'],
                    tx_dict['data'],
                    tx_dict['signature']
                )
                if transaction.tx_hash != tx_dict['tx_hash']:
                    logger.warn("Invalid transaction hash: {} should be {}.  Transaction ignored."
                                .format(tx_dict['tx_hash'], transaction.tx_hash))
                    return None
                return transaction
        except requests.exceptions.RequestException as re:
            logger.warn("Request Exception with host: {}".format(node))
            self.peers.record_downtime(node)
        return None

    def request_transactions_inv(self, node, port, block_hash):
        # Request a list of transaction hashes that belong to a block hash. Used when recreating a block from a
        # block header
        url = self.TRANSACTIONS_INV_URL.format(node, port, block_hash)
        try:
            response = requests.get(url)
            if response.status_code == 200:
                tx_dict = json.loads(response.json())
                return tx_dict['tx_hashes']
        except requests.exceptions.RequestException as re:
            logger.warn("Request Exception with host: {}".format(node))
            self.peers.record_downtime(node)
        return None

    def request_blocks_inv(self, node, port, start_height, stop_height):
        # Used when a synchronization between peers is needed
        url = self.BLOCKS_INV_URL.format(node, port, start_height, stop_height)
        try:
            response = requests.get(url)
            if response.status_code == 200:
                block_dict = json.loads(response.json())
                return block_dict['block_hashes']
        except requests.exceptions.RequestException as re:
            logger.warn("Request Exception with host: {}".format(node))
            self.peers.record_downtime(node)
        return None

    def broadcast_block_inv(self, block_hashes):
        # Used for (re)broadcasting a new block that was received and added
        self.check_peers()
        data = {
            "block_hashes": block_hashes
        }
        for node in self.peers.get_all_peers():
            url = self.INBOX_URL.format(node, self.FULL_NODE_PORT)
            try:
                response = requests.post(url, json=data)
            except requests.exceptions.RequestException as re:
                logger.warn("Request Exception with host: {}".format(node))
                self.peers.record_downtime(node)
        return

    def broadcast_transaction_inv(self, tx_hashes):
        # Used for (re)broadcasting a new transaction that was received and added
        self.check_peers()
        data = {
            "tx_hashes": tx_hashes
        }
        for node in self.peers.get_all_peers():
            url = self.INBOX_URL.format(node, self.FULL_NODE_PORT)
            try:
                response = requests.post(url, json=data)
            except requests.exceptions.RequestException as re:
                logger.warn("Request Exception with host: {}".format(node))
                self.peers.record_downtime(node)
        return

    def broadcast_block_header(self, block_header):
        # Used only when broadcasting a block header that originated (mined) locally
        self.check_peers()
        data = {
            "block_header": block_header.to_json()
        }
        for node in self.peers.get_all_peers():
            url = self.INBOX_URL.format(node, self.FULL_NODE_PORT)
            try:
                response = requests.post(url, json=data)
            except requests.exceptions.RequestException as re:
                logger.warn("Request Exception with host: {}".format(node))
                self.peers.record_downtime(node)
        return

    def __remove_unconfirmed_transactions(self, transactions):
        self.mempool.remove_unconfirmed_transactions(transactions)

    def mine(self):
        logger.debug("mining node starting on %s with reward address of %s...", self.host, self.reward_address)
        while not self.exit_flag:
            block = self.mine_block(self.reward_address)
            if not block:
                continue
            if self.blockchain.add_block(block):
                self.mempool.remove_unconfirmed_transactions(block.transactions[1:])
                self.broadcast_block_inv(block.block_header.hash)
                logger.info("Block {} found with hash {} and nonce {}"
                            .format(block.height, block.block_header.hash, block.block_header.nonce))
        return


    # def request_blocks_range(self, node, port, start_index, stop_index):
    #     # TODO: Deprecate
    #     # TODO: Limit number of blocks
    #     url = self.BLOCKS_RANGE_URL.format(node, port, start_index, stop_index)
    #     blocks = []
    #     try:
    #         response = requests.get(url)
    #         if response.status_code == 200:
    #             blocks_dict = json.loads(response.json())
    #             for block_dict in blocks_dict:
    #                 block = Block(
    #                     block_dict['index'],
    #                     [Transaction(
    #                         transaction['source'],
    #                         transaction['destination'],
    #                         transaction['amount'],
    #                         transaction['fee'],
    #                         transaction['signature'])
    #                      for transaction in block_dict['transactions']
    #                      ],
    #                     block_dict['previous_hash'],
    #                     block_dict['timestamp'],
    #                     block_dict['nonce']
    #                 )
    #                 if block.current_hash != block_dict['current_hash']:
    #                     raise InvalidHash(block.index, "Block Hash Mismatch: {}".format(block_dict['current_hash']))
    #                 blocks.append(block)
    #     except requests.exceptions.RequestException as re:
    #         pass
    #     return blocks

    # def request_blockchain(self, node, port):
    #     # TODO: Deprecate
    #     url = self.BLOCKS_URL.format(node, port, "")
    #     blocks = []
    #     try:
    #         response = requests.get(url)
    #         if response.status_code == 200:
    #             blocks_dict = json.loads(response.json())
    #             for block_dict in blocks_dict:
    #                 block = Block(
    #                     block_dict['index'],
    #                     [Transaction(
    #                         transaction['source'],
    #                         transaction['destination'],
    #                         transaction['amount'],
    #                         transaction['fee'],
    #                         transaction['signature'])
    #                      for transaction in block_dict['transactions']
    #                      ],
    #                     block_dict['previous_hash'],
    #                     block_dict['timestamp'],
    #                     block_dict['nonce']
    #                 )
    #                 if block.block_header.hash != block_dict['current_hash']:
    #                     raise InvalidHash(block.height, "Block Hash Mismatch: {}".format(block_dict['current_hash']))
    #                 blocks.append(block)
    #             return blocks
    #     except requests.exceptions.RequestException as re:
    #         pass
    #     return None
    # def broadcast_block(self, block):
    #     # TODO DEPRECATE
    #     # TODO convert to grequests and concurrently gather a list of responses
    #     statuses = {
    #         "confirmations": 0,
    #         "invalidations": 0,
    #         "expirations": 0
    #     }
    #
    #     self.check_peers()
    #     bad_nodes = set()
    #     data = {
    #         "block": block.to_json(),
    #         "host": self.host
    #     }
    #
    #     for node in self.full_nodes:
    #         if node == self.host:
    #             continue
    #         url = self.BLOCKS_URL.format(node, self.FULL_NODE_PORT, "")
    #         try:
    #             response = requests.post(url, json=data)
    #             if response.status_code == 202:
    #                 # confirmed and accepted by node
    #                 statuses["confirmations"] += 1
    #             elif response.status_code == 406:
    #                 # invalidated and rejected by node
    #                 statuses["invalidations"] += 1
    #             elif response.status_code == 409:
    #                 # expired and rejected by node
    #                 statuses["expirations"] += 1
    #         except requests.exceptions.RequestException as re:
    #             bad_nodes.add(node)
    #     for node in bad_nodes:
    #         self.remove_node(node)
    #     bad_nodes.clear()
    #     return statuses

    # def add_node(self, host):
    #     # TODO: Deprecate
    #     if host == self.host:
    #         return
    #
    #     if host not in self.full_nodes:
    #         self.broadcast_node(host)
    #         self.full_nodes.add(host)

    # def broadcast_node(self, host):
    #     # TODO: Deprecate
    #     self.check_peers()
    #     bad_nodes = set()
    #     data = {
    #         "host": host
    #     }
    #
    #     for node in self.full_nodes:
    #         if node == self.host:
    #             continue
    #         url = self.NODES_URL.format(node, self.FULL_NODE_PORT)
    #         try:
    #             requests.post(url, json=data)
    #         except requests.exceptions.RequestException as re:
    #             bad_nodes.add(node)
    #     for node in bad_nodes:
    #         self.remove_node(node)
    #     bad_nodes.clear()
    #     return

    # def synchronize(self):
    #     # TODO: Deprecate
    #     my_latest_block = self.blockchain.get_tallest_block_header()
    #     """
    #     latest_blocks = {
    #         index1 : {
    #             current_hash1 : [node1, node2],
    #             current_hash2 : [node3]
    #         },
    #         index2 : {
    #             current_hash3 : [node4]
    #         }
    #     }
    #     """
    #     latest_blocks = {}
    #
    #     self.check_peers()
    #     bad_nodes = set()
    #     for node in self.full_nodes:
    #         url = self.BLOCKS_URL.format(node, self.FULL_NODE_PORT, "latest")
    #         try:
    #             response = requests.get(url)
    #             if response.status_code == 200:
    #                 remote_latest_block = json.loads(response.json())
    #                 if remote_latest_block["index"] <= my_latest_block.index:
    #                     continue
    #                 if latest_blocks.get(remote_latest_block["index"], None) is None:
    #                     latest_blocks[remote_latest_block["index"]] = {
    #                         remote_latest_block["current_hash"]: [node]
    #                     }
    #                     continue
    #                 if latest_blocks[remote_latest_block["index"]].get(remote_latest_block["current_hash"], None) is None:
    #                     latest_blocks[remote_latest_block["index"]][remote_latest_block["current_hash"]] = [node]
    #                     continue
    #                 latest_blocks[remote_latest_block["index"]][remote_latest_block["current_hash"]].append(node)
    #         except requests.exceptions.RequestException as re:
    #             bad_nodes.add(node)
    #     if len(latest_blocks) > 0:
    #         for latest_block in sorted(latest_blocks.items(), reverse=True):
    #             index = latest_block[0]
    #             current_hashes = latest_block[1]
    #             success = True
    #             for current_hash in current_hashes:
    #                 remote_host = current_hash[1][0]
    #
    #                 remote_diff_blocks = self.request_blocks_range(
    #                     remote_host,
    #                     self.FULL_NODE_PORT,
    #                     my_latest_block.index + 1,
    #                     index
    #                 )
    #                 if remote_diff_blocks[0].previous_hash == my_latest_block.current_hash:
    #                     # first block in diff blocks fit local chain
    #                     for block in remote_diff_blocks:
    #                         # TODO: validate
    #                         result = self.blockchain.add_block(block)
    #                         if not result:
    #                             success = False
    #                             break
    #                         else:
    #                             self.__remove_unconfirmed_transactions(block.transactions[1:])
    #                 else:
    #                     # first block in diff blocks does not fit local chain
    #                     for i in range(my_latest_block.index, 1, -1):
    #                         # step backwards and look for the first remote block that fits the local chain
    #                         block = self.request_block(remote_host, self.FULL_NODE_PORT, str(i))
    #                         remote_diff_blocks[0:0] = [block]
    #                         if block.block_header.previous_hash == self.blockchain.get_block_headers_by_height(i-1):
    #                             # found the fork
    #                             result = self.blockchain.alter_chain(remote_diff_blocks)
    #                             success = result
    #                             break
    #                     success = False
    #                 if success:
    #                     break
    #             if success:
    #                 break
    #     return

    # @app.route('/nodes/', methods=['POST'])
    # def post_node(self, request):
    #     # TODO: Deprecate
    #     body = json.loads(request.content.read())
    #     self.add_node(body['host'])
    #     return json.dumps({'success': True})

    # @app.route('/blocks/', methods=['POST'])
    # def post_block(self, request):
    #     # TODO: Deprecate
    #     body = json.loads(request.content.read())
    #     remote_block = json.loads(body['block'])
    #     remote_host = body['host']
    #     block = Block.from_dict(remote_block)
    #     if block.current_hash != remote_block['current_hash']:
    #         request.setResponseCode(406)  # not acceptable
    #         return json.dumps({'message': 'block rejected due to invalid hash'})
    #     my_latest_block = self.blockchain.get_tallest_block_header()
    #
    #     if block.index > my_latest_block.index + 1:
    #         # new block index is greater than ours
    #         remote_diff_blocks = self.request_blocks_range(
    #             remote_host,
    #             self.FULL_NODE_PORT,
    #             my_latest_block.index + 1,
    #             remote_block['index']
    #         )
    #
    #         if remote_diff_blocks[0].previous_hash == my_latest_block.current_hash:
    #             # first block in diff blocks fit local chain
    #             for block in remote_diff_blocks:
    #                 # TODO: validate
    #                 result = self.blockchain.add_block(block)
    #                 if not result:
    #                     request.setResponseCode(406)  # not acceptable
    #                     return json.dumps({'message': 'block {} rejected'.format(block.index)})
    #             self.__remove_unconfirmed_transactions(block.transactions)
    #             request.setResponseCode(202)  # accepted
    #             return json.dumps({'message': 'accepted'})
    #         else:
    #             # first block in diff blocks does not fit local chain
    #             for i in range(my_latest_block.index, 1, -1):
    #                 # step backwards and look for the first remote block that fits the local chain
    #                 block = self.request_block(remote_host, self.FULL_NODE_PORT, str(i))
    #                 remote_diff_blocks[0:0] = [block]
    #                 if block.block_header.previous_hash == self.blockchain.get_block_headers_by_height(i-1):
    #                     # found the fork
    #                     result = self.blockchain.alter_chain(remote_diff_blocks)
    #                     if not result:
    #                         request.setResponseCode(406)  # not acceptable
    #                         return json.dumps({'message': 'blocks rejected'})
    #                     self.__remove_unconfirmed_transactions(block.transactions)
    #                     request.setResponseCode(202)  # accepted
    #                     return json.dumps({'message': 'accepted'})
    #             request.setResponseCode(406)  # not acceptable
    #             return json.dumps({'message': 'blocks rejected'})
    #
    #     elif block.index <= my_latest_block.index:
    #         # new block index is less than ours
    #         request.setResponseCode(409)  # conflict
    #         return json.dumps({'message': 'Block index too low.  Fetch latest chain.'})
    #
    #     # correct block index. verify txs, hash
    #     # TODO: validate
    #     result = self.blockchain.add_block(block)
    #     if not result:
    #         request.setResponseCode(406)  # not acceptable
    #         return json.dumps({'message': 'block {} rejected'.format(block.index)})
    #     self.__remove_unconfirmed_transactions(block.transactions)
    #     request.setResponseCode(202)  # accepted
    #     return json.dumps({'message': 'accepted'})

    # @app.route('/blocks/start/<start_block_id>/end/<end_block_id>', methods=['GET'])
    # def get_blocks_range(self, request, start_block_id, end_block_id):
    #     return json.dumps([block.to_dict() for block in self.blockchain.get_blocks_range(int(start_block_id), int(end_block_id))])


if __name__ == "__main__":
    pass
