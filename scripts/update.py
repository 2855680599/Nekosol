"""Side-by-side, offline updates of the complete nyairo distribution (Linux/WSL)."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = 'L1AN929/nyairo'
TAG = re.compile(r'v\d+\.\d+\.\d+(?:-(?:rc|beta|alpha)\d+)?\Z')
MAX_DOWNLOAD = 512 * 1024 * 1024
MAX_EXPANDED = 2 * 1024 * 1024 * 1024


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf8'))


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    try:
        with temporary.open('x', encoding='utf8') as stream:
            os.chmod(temporary, 0o600)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def private_directory(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('更新目录不能是符号链接：' + str(path))
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.stat().st_uid != os.getuid():
        raise ValueError('更新目录不属于当前账号：' + str(path))
    os.chmod(path, 0o700)
    return path


def release_info(root):
    root = Path(root)
    if not (root / 'scripts/release.json').exists():
        manifest = read_json(root / 'MANIFEST.json')
        baseline = read_json(root / 'patches/baseline.json')['upstream']
        # Only the previously tested privacy release has this migration contract.
        if manifest.get('version') != '0.1.0-rc5':
            raise ValueError('旧版没有兼容的更新记录；请按手动更新教程迁移')
        return {'schema': 1, 'version': 'v0.1.0-rc5', 'profile_format': 1,
                'update_protocol': 1, 'hermes_version': baseline['version'], 'hermes_commit': baseline['commit']}
    info = read_json(root / 'scripts/release.json')
    if (info.get('schema') != 1 or not TAG.fullmatch(info.get('version', '')) or
            info.get('update_protocol') != 1 or type(info.get('profile_format')) is not int):
        raise ValueError('安装包的更新说明不受支持')
    baseline = read_json(Path(root) / 'patches/baseline.json')['upstream']
    if (baseline['commit'] != info.get('hermes_commit') or
            baseline['version'] != info.get('hermes_version')):
        raise ValueError('Hermes 版本记录不一致')
    return info


def verify_source(root):
    path = Path(root) / 'scripts/verify_manifest.py'
    spec = importlib.util.spec_from_file_location('nyairo_source_verifier', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.verify(root)
    if not result['valid']:
        raise ValueError('安装包校验失败：' + json.dumps(result, ensure_ascii=False))
    return release_info(root)


def safe_url(url):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != 'https' or parsed.username or parsed.password or
            parsed.port not in (None, 443) or parsed.hostname not in {
                'api.github.com', 'github.com', 'release-assets.githubusercontent.com',
                'objects.githubusercontent.com'}):
        raise ValueError('下载地址不是受支持的 GitHub HTTPS 地址')
    return url


class ReleaseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers, safe_url(newurl))


def download(url, destination, limit=MAX_DOWNLOAD):
    request = urllib.request.Request(safe_url(url), headers={'User-Agent': 'nyairo-updater/1'})
    opener = urllib.request.build_opener(ReleaseRedirect())
    count = 0
    with opener.open(request, timeout=60) as response, Path(destination).open('xb') as output:
        while chunk := response.read(1024 * 1024):
            count += len(chunk)
            if count > limit:
                raise ValueError('下载文件过大')
            output.write(chunk)


def github_json(url):
    with tempfile.TemporaryDirectory(prefix='nyairo-release-info-') as temporary:
        path = Path(temporary) / 'response.json'
        download(url, path, limit=4 * 1024 * 1024)
        return read_json(path)


def asset_pair(release):
    tag = release.get('tag_name', '')
    if release.get('draft') or not TAG.fullmatch(tag):
        raise ValueError('发行版本无效')
    names = {asset['name']: asset['browser_download_url'] for asset in release.get('assets', [])}
    name = f'nyairo-{tag}.zip'
    urls = (names[name], names[name + '.sha256'])
    prefix = f'https://github.com/{REPOSITORY}/releases/download/{tag}/'
    if any(not url.startswith(prefix) for url in urls):
        raise ValueError('发行附件地址不属于 nyairo')
    return tag, urls


def select_release(version=None, channel='candidate'):
    api = f'https://api.github.com/repos/{REPOSITORY}/releases'
    if version:
        if not TAG.fullmatch(version):
            raise ValueError('版本名称应类似 v0.1.0-rc7')
        return asset_pair(github_json(api + '/tags/' + version))
    releases = github_json(api + '?per_page=100')
    if not isinstance(releases, list) or any(not isinstance(item, dict) for item in releases):
        raise ValueError('GitHub 返回的发行列表无效')
    usable = []
    for release in releases:
        if channel == 'stable' and release.get('prerelease'):
            continue
        try:
            pair = asset_pair(release)
        except (ValueError, KeyError):
            continue
        # GitHub list order follows release creation, not the version being published.
        tag = pair[0]
        usable.append((version_key(tag), pair))
    if not usable:
        raise ValueError('这个发布频道暂时没有可下载的完整发行包')
    return max(usable, key=lambda item: item[0])[1]


def version_key(tag):
    match = re.fullmatch(r'v(\d+)\.(\d+)\.(\d+)(?:-(rc|beta|alpha)(\d+))?', tag)
    rank = {'alpha': 0, 'beta': 1, 'rc': 2, None: 3}[match[4]]
    return int(match[1]), int(match[2]), int(match[3]), rank, int(match[5] or 0)


def extract_archive(archive, destination):
    """Validate all entries before writing any: flat ZIP only, no links or duplicates."""
    destination = Path(destination)
    with zipfile.ZipFile(archive) as source:
        entries = source.infolist()
        if len(entries) > 40000 or sum(entry.file_size for entry in entries) > MAX_EXPANDED:
            raise ValueError('安装包解压后过大')
        seen = set()
        for entry in entries:
            name = entry.orig_filename
            path = PurePosixPath(name)
            mode = entry.external_attr >> 16
            if (not name or '\x00' in name or '\\' in name or ':' in name or path.is_absolute() or
                    any(part in ('..', '.git', '.venv') for part in path.parts) or
                    path.as_posix().rstrip('/') != name.rstrip('/') or
                    name.rstrip('/') in seen or
                    (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR))):
                raise ValueError('安装包包含不安全的路径：' + name)
            seen.add(name.rstrip('/'))
        destination.mkdir(mode=0o700)
        for entry in entries:
            path = destination.joinpath(*PurePosixPath(entry.filename).parts)
            if entry.is_dir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with source.open(entry) as value, path.open('xb') as output:
                    shutil.copyfileobj(value, output)
                os.chmod(path, 0o755 if (entry.external_attr >> 16) & 0o111 else 0o644)


def profile_homes(state):
    candidates = [Path(os.environ.get('HERMES_HOME', Path.home() / '.hermes')).expanduser(), Path.home() / '.hermes']
    candidates += [Path(value) for value in state.get('profiles', [])]
    profiles = Path.home() / '.hermes/profiles'
    if profiles.is_dir():
        candidates += [path for path in profiles.iterdir() if path.is_dir()]
    return sorted({path.resolve() for path in candidates if (path / 'plugins/chiyo').exists()})


def assert_offline(roots, homes):
    """Use exact code/profile paths. Do not stop unrelated services or guess argv substrings."""
    try:
        import psutil
    except ImportError as exc:
        raise ValueError('需要安装依赖后再更新：先运行 bash scripts/install.sh') from exc
    resolved_roots = [str(Path(root).resolve()) for root in roots]
    resolved_homes = {str(home.resolve()) for home in homes}
    for process in psutil.process_iter(['pid', 'uids']):
        if process.pid == os.getpid() or process.pid in {parent.pid for parent in psutil.Process().parents()}:
            continue
        try:
            if process.uids().real != os.getuid():
                continue
            env = process.environ()
            code_paths = env.get('PYTHONPATH', '').split(os.pathsep)
            home = env.get('HERMES_HOME')
            argv = process.cmdline()
            # Absolute executable/script arguments under a release also cover old launchers.
            code_holder = any(value == root or value.startswith(root + os.sep)
                              for root in resolved_roots for value in argv if os.path.isabs(value))
            if (code_holder or any(root in code_paths for root in resolved_roots) or
                    (home and str(Path(home).expanduser().resolve()) in resolved_homes)):
                raise ValueError(f'程序还在运行（PID {process.pid}）。先退出聊天、停止消息网关和附加服务，再重试。')
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied as exc:
            raise ValueError(f'无法确认 PID {process.pid} 是否已停止；更新未执行') from exc


def plugin_files(directory):
    values = {}
    directory = Path(directory)
    for folder, dirs, files in os.walk(directory, followlinks=False):
        dirs[:] = [name for name in dirs if name != '__pycache__']
        base = Path(folder)
        if any((base / name).is_symlink() for name in dirs + files):
            raise ValueError('个人插件中有符号链接；请先人工检查')
        for name in files:
            path = base / name
            values[path.relative_to(directory).as_posix()] = path.read_bytes()
    return values


def check_plugins(current, homes):
    expected = plugin_files(Path(current) / 'plugins/chiyo')
    for home in homes:
        if plugin_files(home / 'plugins/chiyo') != expected:
            raise ValueError(f'{home}/plugins/chiyo 有自定义修改；更新未覆盖，请先保存并核对这些修改')


def backup_profiles(homes, backup):
    for index, home in enumerate(homes):
        # Archive includes config, SOUL, secrets, history, memory and journals. Links are not followed.
        with tarfile.open(backup / f'profile-{index}.tar.gz', 'w:gz', dereference=False) as archive:
            def include(entry):
                if entry.type not in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE, tarfile.SYMTYPE, tarfile.LNKTYPE):
                    return None  # socket endpoints are recreated by services, never personal data
                return entry
            archive.add(home, arcname='profile', filter=include)
        os.chmod(backup / f'profile-{index}.tar.gz', 0o600)
        shutil.copytree(home / 'plugins/chiyo', backup / f'plugin-{index}', ignore=shutil.ignore_patterns('__pycache__'))
    atomic_json(backup / 'profiles.json', [str(home) for home in homes])


def replace_plugin(home, source):
    parent = home / 'plugins'
    prepared = parent / ('.nyairo-new-' + uuid.uuid4().hex)
    old = parent / ('.nyairo-old-' + uuid.uuid4().hex)
    shutil.copytree(source, prepared, ignore=shutil.ignore_patterns('__pycache__'))
    try:
        existed = (parent / 'chiyo').exists()
        if existed:
            os.rename(parent / 'chiyo', old)
        try:
            os.rename(prepared, parent / 'chiyo')
        except BaseException:
            if existed:
                os.rename(old, parent / 'chiyo')
            raise
    finally:
        if prepared.exists():
            shutil.rmtree(prepared)
    if old.exists():
        shutil.rmtree(old)


def install_runtime(root):
    uv = shutil.which('uv')
    if not uv:
        raise ValueError('找不到 uv；请先按安装教程安装 uv')
    subprocess.run([uv, 'sync', '--frozen', '--extra', 'messaging', '--extra', 'web'],
                   cwd=Path(root) / 'vendor/hermes', check=True)
    python = Path(root) / 'vendor/hermes/.venv/bin/python'
    env = {**os.environ, 'PYTHONPATH': str(root), 'PYTHONDONTWRITEBYTECODE': '1'}
    with tempfile.TemporaryDirectory(prefix='nyairo-upgrade-probe-') as temporary:
        env['HERMES_HOME'] = temporary
        subprocess.run([str(python), str(Path(root) / 'scripts/check_update_runtime.py')],
                       cwd=root, env=env, check=True)


class Updater:
    def __init__(self, root=ROOT, state_dir=None):
        self.root = Path(root).resolve()
        self.directory = private_directory(state_dir or os.environ.get('NYAIRO_STATE_DIR') or Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'nyairo')
        self.state_path = self.directory / 'install.json'
        self.journal_path = self.directory / 'pending.json'
        self.state = read_json(self.state_path) if self.state_path.exists() else {}

    @contextlib.contextmanager
    def locked(self):
        import fcntl
        with (self.directory / 'update.lock').open('a') as lock:
            os.chmod(self.directory / 'update.lock', 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError('另一个更新正在进行，请稍后重试') from exc
            self.state = read_json(self.state_path) if self.state_path.exists() else {}
            yield

    def recover(self):
        if not self.journal_path.exists():
            return
        pending = read_json(self.journal_path)
        if self.state.get('transaction') != pending['transaction']:
            homes = [Path(value) for value in pending['profiles']]
            assert_offline([pending['old_root'], pending['new_root']], homes)
            backup = Path(pending['backup'])
            for index, home in enumerate(homes):
                replace_plugin(home, backup / f'plugin-{index}')
            print('已恢复上次中断更新的插件；个人数据未回退。')
        self.journal_path.unlink()

    def launcher(self):
        launcher = Path.home() / '.local/bin/nyairo'
        launcher.parent.mkdir(parents=True, exist_ok=True)
        marker = '# nyairo managed launcher v1\n'
        if launcher.is_symlink() or (launcher.exists() and launcher.read_text(encoding='utf8').splitlines()[1:2] != [marker.strip()]):
            raise ValueError(str(launcher) + ' 已被其他程序占用')
        value = ('#!/usr/bin/env bash\n' + marker + 'set -euo pipefail\n'
                 'export NYAIRO_STATE_DIR=' + shlex.quote(str(self.directory)) + '\n'
                 'if [[ -z "${HERMES_HOME:-}" ]]; then export HERMES_HOME=' + shlex.quote(self.state.get('default_profile', str(Path.home() / '.hermes'))) + '; fi\n'
                 'root=$(' + shlex.quote(str(Path(sys.executable).resolve())) + ' -c ' + shlex.quote('import json; print(json.load(open(' + repr(str(self.state_path)) + '))[' + repr('current') + '])') + ')\n'
                 'if [[ -e ' + shlex.quote(str(self.journal_path)) + ' && "${1:-}" != update ]]; then\n'
                 '  echo "上次更新中断，请先运行 ~/.local/bin/nyairo update --recover" >&2\n  exit 1\nfi\n'
                 'if [[ "${1:-}" == update ]]; then\n  shift\n'
                 '  if [[ -f "$root/scripts/update.sh" ]]; then exec bash "$root/scripts/update.sh" "$@"; fi\n'
                 '  exec bash ' + shlex.quote(str(self.root / 'scripts/update.sh')) + ' "$@"\nfi\n'
                 'exec bash "$root/scripts/hermes.sh" "$@"\n')
        temporary = launcher.with_name('.nyairo-' + uuid.uuid4().hex)
        temporary.write_text(value, encoding='utf8')
        os.chmod(temporary, 0o755)
        os.replace(temporary, launcher)
        return launcher

    def adopt(self, releases_dir=None):
        info = verify_source(self.root)
        current = self.state.get('current')
        if current and Path(current).resolve() != self.root:
            raise ValueError('已有统一安装。请运行 ~/.local/bin/nyairo update，避免手动覆盖更新记录。')
        homes = profile_homes(self.state)
        assert_offline([self.root], homes)
        self.state = {**self.state, 'current': str(self.root), 'version': info['version'],
                      'profiles': [str(home) for home in homes],
                      'default_profile': str(Path(os.environ.get('HERMES_HOME', Path.home() / '.hermes')).expanduser().resolve())}
        if releases_dir:
            self.state['releases_dir'] = str(private_directory(releases_dir).resolve())
        launcher = self.launcher()
        atomic_json(self.state_path, self.state)
        print(f'统一入口已安装：{launcher}\n以后更新：{launcher} update')

    def migrate(self, previous):
        previous = Path(previous).expanduser().resolve()
        if self.state and Path(self.state.get('current', '')).resolve() != previous:
            raise ValueError('已有统一安装，请直接运行 nyairo update')
        info = verify_source(previous)
        homes = profile_homes({})
        assert_offline([previous, self.root], homes)
        self.state = {'current': str(previous), 'version': info['version'], 'profiles': [str(home) for home in homes],
                      'default_profile': str(Path(os.environ.get('HERMES_HOME', Path.home() / '.hermes')).expanduser().resolve())}
        atomic_json(self.state_path, self.state)
        self.activate(self.root)

    def activate(self, target, runtime=install_runtime):
        current = Path(self.state.get('current', self.root)).resolve()
        old_info = verify_source(current)
        target = Path(target).resolve()
        new_info = verify_source(target)
        if new_info['profile_format'] != old_info['profile_format']:
            raise ValueError('这个版本需要单独的数据迁移，不能自动更新')
        homes = profile_homes(self.state)
        assert_offline([current, target], homes)
        check_plugins(current, homes)
        runtime(target)  # install at its FINAL path; editable venv paths must never be moved
        verify_source(target)
        assert_offline([current, target], homes)
        check_plugins(current, homes)
        self.launcher()  # occupation/permission errors occur before any personal plugin swap
        backup = private_directory(self.directory / ('backups/' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]))
        backup_profiles(homes, backup)
        transaction = uuid.uuid4().hex
        atomic_json(self.journal_path, {'transaction': transaction, 'old_root': str(current),
                                      'new_root': str(target), 'profiles': [str(home) for home in homes], 'backup': str(backup)})
        try:
            for home in homes:
                replace_plugin(home, target / 'plugins/chiyo')
            value = {**self.state, 'current': str(target), 'previous': str(current), 'version': new_info['version'],
                     'profiles': [str(home) for home in homes], 'backup': str(backup), 'transaction': transaction}
            atomic_json(self.state_path, value)  # one durable activation point for ALL launchers
            self.state = value
        except BaseException:
            # Reload on a post-replace fsync error: state publication may already be committed.
            self.state = read_json(self.state_path) if self.state_path.exists() else {}
            self.recover()
            raise
        self.journal_path.unlink(missing_ok=True)
        print(f'已更新到 {new_info["version"]}（配套 Hermes {new_info["hermes_version"]}）。\n备份：{backup}\n人设、账号设置、聊天和记忆已保留。请用 ~/.local/bin/nyairo 启动。')

    def rollback(self):
        if not self.state.get('previous'):
            raise ValueError('还没有可退回的上一版')
        previous = Path(self.state['previous'])
        current = Path(self.state['current'])
        if verify_source(previous)['profile_format'] != verify_source(current)['profile_format']:
            raise ValueError('数据格式已变化，不能直接退回旧程序')
        homes = [Path(value) for value in self.state['profiles']]
        assert_offline([current, previous], homes)
        check_plugins(current, homes)
        # Go through the same journalled transaction; never restore old conversation databases.
        self.activate(previous, runtime=lambda target: None)
        print('已退回上一版程序。升级后新增的聊天和记忆仍保留；旧数据备份没有自动覆盖现在的数据。')

    def update(self, version=None, channel='candidate', check=False):
        tag, urls = select_release(version, channel)
        installed = self.state.get('version', release_info(self.root)['version'])
        print(f'当前：{installed}；可用配套版本：{tag}')
        if check or tag == installed:
            return
        if version is None and version_key(tag) < version_key(installed):
            print('已安装的版本更新；没有自动降级。')
            return
        releases = private_directory(self.state.get('releases_dir') or Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')) / 'nyairo/releases')
        destination = releases / (tag + '-' + uuid.uuid4().hex[:8])
        with tempfile.TemporaryDirectory(prefix='nyairo-download-', dir=self.directory) as temporary:
            archive, checksum = Path(temporary) / 'release.zip', Path(temporary) / 'checksum'
            print('正在下载完整 nyairo 安装包（已包含配套 Hermes）……')
            download(urls[0], archive)
            download(urls[1], checksum, limit=4096)
            values = checksum.read_text(encoding='utf8').split()
            if not values:
                raise ValueError('发行包缺少校验值')
            expected = values[0]
            if not re.fullmatch('[0-9a-f]{64}', expected) or hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
                raise ValueError('下载校验失败；当前程序没有改动')
            extract_archive(archive, destination)
        try:
            if release_info(destination)['version'] != tag:
                raise ValueError('下载包与发布标签不一致')
            self.activate(destination)
        except BaseException:
            # Keep staged trees for diagnosis; they never become current until publication.
            print('更新未完成；已下载的程序留在：' + str(destination), file=sys.stderr)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--check', action='store_true', help='只检查可用版本')
    action.add_argument('--adopt', action='store_true', help='为当前安装建立统一入口')
    action.add_argument('--rollback', action='store_true', help='退回上一版程序，保留现在的数据')
    action.add_argument('--recover', action='store_true', help='仅恢复中断的更新，不下载新版')
    action.add_argument('--migrate-from', type=Path, help='把已安装的 rc5 接入统一更新；路径指向旧程序目录')
    parser.add_argument('--version', help='指定已发布的 nyairo 版本')
    parser.add_argument('--channel', choices=['candidate', 'stable'], default='candidate')
    parser.add_argument('--releases-dir', type=Path, help='初次登记时指定后续版本的保存目录')
    args = parser.parse_args()
    if os.name != 'posix' or not Path('/proc').is_dir():
        parser.error('统一更新目前支持 Linux 和 Windows 的 WSL；macOS 和原生 Windows 请按手动更新教程操作')
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    sys.dont_write_bytecode = True
    try:
        updater = Updater()
        with updater.locked():
            if args.check and updater.journal_path.exists():
                raise ValueError('上次更新中断，请先运行 nyairo update --recover')
            updater.recover()
            if args.adopt:
                updater.adopt(args.releases_dir)
            elif args.rollback:
                updater.rollback()
            elif args.recover:
                print('更新状态已恢复，可以正常启动。')
            elif args.migrate_from:
                updater.migrate(args.migrate_from)
            else:
                updater.update(args.version, args.channel, args.check)
        return 0
    except (ValueError, OSError, subprocess.SubprocessError, KeyError, zipfile.BadZipFile) as exc:
        print('更新未执行或未完成：' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
