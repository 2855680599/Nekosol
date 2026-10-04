"""No-follow target access: replacement never opens a destination symlink."""
import os,stat,uuid
from pathlib import Path

class Target:
    def __init__(self,root,stack):
        if not hasattr(os,'O_NOFOLLOW') or os.open not in os.supports_dir_fd:
            raise RuntimeError('Patch management requires Linux/WSL no-follow file operations')
        self.stack=stack;self.root=self._open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    def _open(self,path,flags,**kwargs):
        fd=os.open(path,flags,**kwargs);self.stack.callback(os.close,fd);return fd
    def parent(self,name,create=False):
        parts=Path(name).parts
        if not parts or Path(name).is_absolute() or any(p in ('.','..') for p in parts):raise ValueError('invalid patch path')
        fd=self.root
        for part in parts[:-1]:
            if create:
                try:os.mkdir(part,0o755,dir_fd=fd)
                except FileExistsError:pass
            fd=self._open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
        return fd,parts[-1]
    def read(self,name):
        try:
            fd,leaf=self.parent(name)
            opened=os.open(leaf,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
        except FileNotFoundError:return None
        with os.fdopen(opened,'rb') as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):raise ValueError('patch target is not a regular file')
            return handle.read()
    def publish(self,name,value):
        fd,leaf=self.parent(name,create=value is not None)
        if value is None:os.unlink(leaf,dir_fd=fd);return
        temporary='.nyairo-patch-'+uuid.uuid4().hex
        opened=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o644,dir_fd=fd)
        try:
            with os.fdopen(opened,'wb') as handle:
                handle.write(value);handle.flush();os.fsync(handle.fileno())
            os.replace(temporary,leaf,src_dir_fd=fd,dst_dir_fd=fd)
        finally:
            try:os.unlink(temporary,dir_fd=fd)
            except FileNotFoundError:pass
