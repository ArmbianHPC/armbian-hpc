# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Prepare only the pinned inputs selected by a target, atomically and with hashes."""

from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import urllib.request

from .config import ConfigError, https_url, require, sha256, workspace_path
from .staging import write_json


def git_executable():
    command = shutil.which('git')
    if command:
        return command
    windows_git = Path(r'C:\Program Files\Git\cmd\git.exe')
    require(windows_git.is_file(), '需要 Git；请先安装 Git 或加入 PATH。')
    return str(windows_git)


def git_command(directory, *arguments, **kwargs):
    command = [git_executable(), '--no-optional-locks', '-c',
               'safe.directory=' + directory.resolve().as_posix(), '-C', str(directory), *arguments]
    result = subprocess.run(command, stderr=subprocess.PIPE, **kwargs)
    if result.returncode:
        raise ConfigError('Git 操作失败：' + result.stderr.decode('utf-8', errors='replace').strip())
    return result


class HTTPSRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        https_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url, destination):
    https_url(url)
    request = urllib.request.Request(url, headers={'User-Agent': 'armbian-hpc-source-fetch/0.1'})
    opener = urllib.request.build_opener(HTTPSRedirects())
    with opener.open(request, timeout=60) as response, destination.open('wb') as stream:
        https_url(response.url)
        shutil.copyfileobj(response, stream, length=1024 * 1024)


def make_git_archive(root, acquisition, destination):
    repository = workspace_path(root, acquisition['git_dir'], exists=False)
    if not repository.exists():
        repository.mkdir(parents=True)
        git_command(repository, 'init', '--bare', stdout=subprocess.PIPE)
    exists = subprocess.run([git_executable(), '-c', 'safe.directory=' + repository.as_posix(),
                             '-C', str(repository), 'cat-file', '-e', acquisition['commit'] + '^{commit}'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if not exists:
        git_command(repository, 'fetch', '--no-tags', '--depth=1', acquisition['url'],
                    acquisition['commit'], stdout=subprocess.PIPE)
    with destination.open('wb') as stream:
        # git archive also applies core.autocrlf; force portable LF only for this command.
        git_command(repository, '-c', 'core.autocrlf=false', '-c', 'core.eol=lf',
                    'archive', '--format=tar', '--prefix=' + acquisition['prefix'],
                    acquisition['commit'], stdout=stream)


def prepare_sources(root, resolved, *, verify_only=False):
    root = Path(root).resolve()
    receipt = {'schema_version': 1, 'target': resolved['target']['id'],
               'architecture': resolved['architecture'], 'inputs': []}
    for item in resolved['inputs'] + resolved['firmware']:
        path = workspace_path(root, item['path'], exists=False)
        acquisition = item['acquire']
        if path.is_file():
            require(sha256(path) == item['sha256'], f'输入校验失败；保留文件供排查：{item["path"]}')
            method = 'existing'
        else:
            require(not verify_only, f'缺少本地输入：{item["path"]}')
            require(acquisition['type'] != 'repository', f'缺少仓库中的源码：{item["path"]}')
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(prefix='.' + path.name + '-', suffix='.part', dir=path.parent)
            os.close(descriptor)
            temporary = Path(name)
            try:
                method = None
                for relative in item.get('reuse_paths', []):
                    existing = workspace_path(root, relative, exists=False)
                    if existing.is_file() and sha256(existing) == item['sha256']:
                        shutil.copyfile(existing, temporary)
                        method = 'reused:' + relative
                        break
                if method is None:
                    if acquisition['type'] == 'https':
                        download(acquisition['url'], temporary)
                        method = 'https'
                    else:
                        make_git_archive(root, acquisition, temporary)
                        method = 'git-archive'
                require(sha256(temporary) == item['sha256'], f'下载或归档 SHA256 不匹配：{item["path"]}')
                require(not path.exists(), f'输入已被其他进程创建：{item["path"]}')
                temporary.replace(path)
            finally:
                if temporary.exists():
                    temporary.unlink()
        receipt['inputs'].append({'id': item['id'], 'owner': item['owner'], 'path': item['path'],
                                  'sha256': item['sha256'], 'size_bytes': path.stat().st_size,
                                  'verified': True, 'method': method, 'acquire': acquisition})
    receipt['created_utc'] = datetime.now(timezone.utc).isoformat()
    if not verify_only:
        write_json(workspace_path(root, 'build/source-receipts/' + resolved['target']['id'] + '.json', exists=False), receipt)
    return receipt
