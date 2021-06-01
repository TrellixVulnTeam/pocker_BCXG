"""Docker From Scratch Workshop - Level 1: Chrooting into an image.

Goal: Have your own private hostname!

Usage:
    running:
        sudo /venv/bin/python pocker.py run -i ubuntu bash -- -c hostname
"""

from __future__ import print_function

import click
import os
import traceback
import tarfile
import uuid
import stat

import linux


def _get_image_path(image_name, image_dir, image_suffix='tar'):
    return os.path.join(image_dir, os.extsep.join([image_name, image_suffix]))


def _get_container_path(container_id, container_dir, *subdir_names):
    return os.path.join(container_dir, container_id, *subdir_names)


def create_container_root(image_name, image_dir, container_id, container_dir):
    """
    @param image_name: the image name to extract
    @param image_dir: the directory to lookup image tarballs in
    @param container_id: the unique container id
    @param container_dir: the base directory of newly generated container
                          directories
    @retrun: new container root directory
    @rtype: str
    """
    image_path = _get_image_path(image_name, image_dir)
    assert os.path.exists(image_path), "unable to locate image %s" % image_name

    # 判断该镜像是否已被解压，已被解压的镜像文件可作为lower层，供所有相关容器复用
    # 如此既节省了容器的存储空间由加快了容器的启动速度（不用再解压镜像了）
    image_root = os.path.join(image_dir, image_name, 'rootfs')
    if not os.path.exists(image_root):
        os.makedirs(image_root)
        # 使用 with 自动进行文件对象的清理
        # 解压缩镜像文件压缩包到该容器目录中
        with tarfile.open(image_path) as t:
            # Fun fact: tar files may contain *nix devices! *facepalm*
            members = [m for m in t.getmembers()
               if m.type not in (tarfile.CHRTYPE, tarfile.BLKTYPE)]
            t.extractall(image_root, members=members)
        
    # 创建 overlay 文件系统所需的目录
    # 挂载点，提供 lower 和 upper 的 merge 视图
    container_root = _get_container_path(container_id, container_dir, 'rootfs')
    # 容器读写层
    container_diff = _get_container_path(container_id, container_dir, 'diff')
    # overlay 必须的辅助工作目录
    container_worker = _get_container_path(container_id, container_dir, 'worker')
    for dir in (container_root, container_diff, container_worker):
        if not os.path.exists(dir):
            os.makedirs(dir)

    # 在 container/<container-id>/rootfs 处挂载堆叠文件系统 overlay
    linux.mount('overlay', container_root, 'overlay', linux.MS_NODEV,
                "lowerdir={image_root},upperdir={diff},workdir={workdir}".format(image_root=image_root, diff=container_diff, workdir=container_worker))

    # 返回 overlayfs 文件系统的挂载点，即 lower 和 upper 的 merge 目录
    return container_root


@click.group()
def cli():
    pass


def makedev(dev_path):
    # 添加一些基础的设备
    # 挂载 pts
    devpts_path = os.path.join(dev_path, 'pts')
    if not os.path.exists(devpts_path):
        os.makedirs(devpts_path)
        linux.mount('devpts', devpts_path, 'devpts', 0, '')

    # 通过软链接挂载标准输入输出流设备
    for i, dev in enumerate(['stdin', 'stdout', 'stderr']):
        os.symlink('/proc/self/fd/%d' % i, os.path.join(dev_path, dev))

    os.symlink('/proc/self/fd', os.path.join(dev_path, 'fd'))

    # 挂载其他设备 设备名:(设备类型，主设备号，次设备号)
    # 其中主次设备号可以在 host 的 /dev 下使用 ls -l 查看
    devices = {
        'null': (stat.S_IFCHR, 1, 3),
        'zero': (stat.S_IFCHR, 1, 5),
        'random': (stat.S_IFCHR, 1, 8),
        'urandom': (stat.S_IFCHR, 1, 9),
        'console': (stat.S_IFCHR, 5, 1),
        'tty': (stat.S_IFCHR, 5, 0),
        'full': (stat.S_IFCHR, 1, 7),
    }
    # 遍历设备字典，使用mknod在设备目录下新建设备文件
    for device, (dev_type, major, minor) in devices.items():
        os.mknod(os.path.join(dev_path, device), 0o666 |
                 dev_type, os.makedev(major, minor))


def contain(command, image_name, image_dir, container_id, container_dir):
    # 给当前进程创建 mount namespace
    linux.unshare(linux.CLONE_NEWNS)
    # 将 host 的根目录挂载状态改为私有的，保证内部 mount ns 挂载操作不会传播到 host
    linux.mount(None, '/', None, linux.MS_PRIVATE | linux.MS_REC, None)
    
    # 给当前进程创建 UTS namespace，主机名和域名隔离
    linux.unshare(linux.CLONE_NEWUTS)
    # 修改 hostname 为容器 id
    linux.sethostname(container_id)

    new_root = create_container_root(
        image_name, image_dir, container_id, container_dir)
    print('Created a new root fs for our container: {}'.format(new_root))

    # 在新的根目录下重新挂载 /proc、/sys
    # 挂载 /proc 后就可以使用 ps 命令了，此时可以看到所有的进程，还未隔离
    linux.mount('proc', os.path.join(new_root, 'proc'), 'proc', 0, '')
    linux.mount('sysfs', os.path.join(new_root, 'sys'), 'sysfs', 0, '')

    # 挂载一个 tmpfs 到 root/dev/ 下作为设备目录
    dev_path = os.path.join(new_root, 'dev')
    linux.mount('tmpfs', dev_path, 'tmpfs', linux.MS_NOSUID |
                linux.MS_STRICTATIME, 'mode=755')
    # 在设备目录下添加设备
    makedev(dev_path)

    # 将当前进程所在mount ns的所有进程的根文件系统变为容器目录，该函数需要root权限
    old_root = os.path.join(new_root, 'old_root')
    os.makedirs(old_root) # 创建临时文件夹用来存放老的根目录
    linux.pivot_root(new_root, old_root)
    # 跳转到新的根目录下
    os.chdir('/')
    # 卸载老的根目录，并删除临时文件夹
    linux.umount2('/old_root', linux.MNT_DETACH)
    os.rmdir('/old_root')

    # execvp 函数能够自动从 $PATH 中寻找匹配的命令
    os.execvp(command[0], command)


@cli.command(context_settings=dict(ignore_unknown_options=True,))
@click.option('--image-name', '-i', help='Image name', default='ubuntu')
@click.option('--image-dir', help='Images directory', default='./_pocker/images')
@click.option('--container-dir', help='Containers directory', default='./_pocker/containers')
@click.argument('Command', required=True, nargs=-1)
def run(image_name, image_dir, container_dir, command):
    # 为此次启动的容器确定一个随机的 id
    contain_id = str(uuid.uuid4())
    pid = os.fork()
    if pid == 0:
        # This is the child, we'll try to do some containment here
        try:
            contain(command, image_name, image_dir, contain_id, container_dir)
        except Exception:
            traceback.print_exc()
            os._exit(1)  # something went wrong in contain()

    # This is the parent, pid contains the PID of the forked process
    # wait for the forked child and fetch the exit status
    _, status = os.waitpid(pid, 0)
    print('{} exited with status {}'.format(pid, status))


if __name__ == '__main__':
    cli()
