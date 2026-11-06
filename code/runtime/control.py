import signal, threading
from pathlib import Path
from runtime.artifacts import save

class PauseRequested(RuntimeError):pass

class Controller:
    def __init__(self,run):
        self.flag=Path(run)/'state/pause.request.json'
        self.seen=False
    def request(self,*args):
        save(self.flag,{'request':'pause_after_inflight_requests'})
    def install_signal(self):
        signal.signal(signal.SIGINT,self.request)
    def check(self):
        if self.flag.exists():
            self.seen=True
            raise PauseRequested('Pause requested; no new model work will be dispatched')
    def requested(self):return self.flag.exists()

def disable_network():
    import requests,socket,ipaddress
    def blocked(*args,**kwargs):raise RuntimeError('Network disabled: replay/local execution')
    requests.sessions.Session.request=blocked
    original_connect=socket.socket.connect
    original_connect_ex=socket.socket.connect_ex
    original_pair=socket.socketpair
    local=threading.local()
    def guarded(original):
        def connect(sock,address):
            # Windows implements socketpair using a loopback connection. Only the
            # current thread inside this specific library operation may use it.
            if getattr(local,'socketpair',False) and isinstance(address,tuple) and ipaddress.ip_address(address[0]).is_loopback:
                return original(sock,address)
            return blocked()
        return connect
    def pair(*args,**kwargs):
        previous=getattr(local,'socketpair',False);local.socketpair=True
        try:return original_pair(*args,**kwargs)
        finally:local.socketpair=previous
    socket.socket.connect=guarded(original_connect)
    socket.socket.connect_ex=guarded(original_connect_ex)
    socket.socketpair=pair
