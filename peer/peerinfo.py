class PeerInfo:
    def __init__(self, peer_id, peer_data):
        self.peer_id = peer_id
        self.addrs = peer_data.get_addrs()
