"""Atomic single-writer admission, inherited from fresh launcher through systemd scope."""
import fcntl
import os
from pathlib import Path
import stat


def lock_path():
    default=Path(os.environ.get('XDG_RUNTIME_DIR','/tmp'))/('le-chonk-harness-%d.lock'%os.getuid())
    return Path(os.environ.get('LECHONK_LOCK_FILE',default))


def acquire():
    path=lock_path()
    inherited=os.environ.get('LECHONK_LOCK_FD')
    if inherited:
        try:
            fd=int(inherited);opened=os.fstat(fd);expected=path.stat()
            if (opened.st_dev,opened.st_ino)!=(expected.st_dev,expected.st_ino):raise ValueError()
        except (ValueError,OSError):
            raise RuntimeError('Invalid inherited harness ownership lock') from None
    else:
        fd=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    info=os.fstat(fd)
    if info.st_uid!=os.getuid() or not stat.S_ISREG(info.st_mode):
        if not inherited:os.close(fd)
        raise RuntimeError('Unsafe harness ownership lock file')
    try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        if not inherited:os.close(fd)
        raise RuntimeError('Another process already owns the game writer lock') from None
    os.fchmod(fd,0o600)
    os.set_inheritable(fd,True)
    os.environ['LECHONK_LOCK_FD']=str(fd)
    return fd
