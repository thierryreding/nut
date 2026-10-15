#!/usr/bin/env python3

import click, git, json, os, os.path, paramiko, pathlib, re, yaml
import nut

from dataclasses import dataclass, field
from typing import List, Optional

from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.table import Column

@dataclass
class Remote:
    name: str
    branch: str
    url: str
    fetch: List[str] = field(default_factory = list)
    pushurl: Optional[str] = None
    push: List[str] = field(default_factory = list)

@dataclass
class Repository:
    path: str
    bare: bool = False
    reference: Optional[str] = None
    remotes: List[Remote] = field(default_factory = list)

    @classmethod
    def from_dict(cls, data: dict) -> Repository:
        remotes_data = data.get('remotes', [])
        remotes = [
                Remote(
                    name = r['name'],
                    branch = r.get('branch', 'master'),
                    url = r.get('url', None),
                    fetch = r.get('fetch', []),
                    pushurl = r.get('pushurl', None),
                    push = r.get('push', [])
                ) for r in remotes_data
            ]

        return cls(path = data['path'],
                   bare = data.get('bare', False),
                   reference = data.get('reference', None),
                   remotes = remotes)

class NutContext:
    def __init__(self):
        self.topdir = pathlib.Path.cwd()
        self.repo = None

def check_repository(ctx):
    check_files = [ 'Makefile', 'MAINTAINERS', 'Kconfig' ]

    for file in check_files:
        path = ctx.obj.topdir / file

        if not path.exists():
            click.echo(f'ERROR: file {file} not found, this tool must be run from a Linux kernel repository', err = True)
            ctx.exit(1)

    try:
        ctx.obj.repo = git.Repo('.')
    except git.InvalidGitRepositoryError:
        click.echo('ERROR: not a git repository', err = True)
        ctx.exit(1)

@click.group
@click.pass_context
def cli(ctx):
    if ctx.invoked_subcommand != 'setup':
        check_repository(ctx)

KERNEL_GIT = 'git://git.kernel.org'
KERNEL_SSH = 'ssh://git@gitolite.kernel.org'

repositories = [
        Repository(
            path = 'etc',
            bare = False,
            remotes = [
                Remote(
                    name = 'origin',
                    branch = 'main',
                    url = f'{KERNEL_GIT}/pub/scm/utils/next-utils/etc.git',
                    fetch = [ '+refs/heads/*:refs/remote/origin/*' ],
                    pushurl = f'{KERNEL_SSH}:pub/scm/utils/next-utils/etc.git'
                )
            ]
        ),
    ]

class StatusSpinnerColumn(SpinnerColumn):
    def render(self, task):
        if task.finished:
            if task.fields.get('failed'):
                return '[bold red]✗[/bold red]'

            return '[bold green]✓[/bold green]'

        return super().render(task)

def setup_repository(topdir, repository: Repository):
    path = topdir / repository.path

    progress = Progress(
            StatusSpinnerColumn(),
            TextColumn('{task.description}', table_column = Column(ratio = 1)),
            BarColumn(),
            TaskProgressColumn(),
            expand = True,
        )

    with progress:
        if not path.exists():
            if repository.reference:
                reference = topdir / repository.reference
            else:
                reference = None

            for remote in repository.remotes:
                if remote.name == 'origin':
                    message = f'cloning {remote.url} into {path}'

                    task = progress.add_task(message, total = 100)
                    handler = nut.GitProgressHandler(progress, task)

                    repo = git.Repo.clone_from(remote.url, path,
                                               bare = repository.bare,
                                               reference = reference,
                                               branch = remote.branch,
                                               progress = handler)

                    progress.update(task, completed = 100, total = 100,
                                    description = message)
                    break
            else:
                raise Exception(f'ERROR: no origin remote for repository {url}')
        else:
                message = f'reusing existing repository in {path}'

                task = progress.add_task(message, total = 100)
                repo = git.Repo(path)

                progress.update(task, completed = 100, total = 100,
                                description = message)

        for remote in repository.remotes:
            message = f'└─ setting up remote {remote.name}'
            task = progress.add_task(message, total = 100)

            if remote.name not in repo.remotes:
                rem = repo.create_remote(remote.name, remote.url)

            with repo.config_writer() as config:
                section = f'remote "{remote.name}"'

                config.set_value(section, 'url', remote.url)

                if remote.fetch:
                    add_value = config.set_value

                    for refspec in remote.fetch:
                        add_value(section, 'fetch', refspec)
                        add_value = config.add_value

                if remote.pushurl:
                    config.set_value(section, 'pushurl', remote.pushurl)

                if remote.push:
                    add_value = config.set_value

                    for refspec in remote.push:
                        add_value(section, 'push', refspec)
                        add_value = config.add_value

            progress.update(task, completed = 100, total = 100,
                            description = message)

@cli.command
@click.pass_context
def setup(ctx):
    for repository in repositories:
        setup_repository(ctx.obj.topdir, repository)

    with open(ctx.obj.topdir / 'etc' / 'setup.yml', 'r') as file:
        data = yaml.safe_load(file)

        if 'repositories' in data:
            for repository in data['repositories']:
                repository = Repository.from_dict(repository)
                setup_repository(ctx.obj.topdir, repository)

    #obj.control = nut.ControlFile.load(obj.repo)

@cli.group
@click.pass_context
def trees(ctx):
    ctx.obj.state = nut.StateCache()

    ctx.call_on_close(ctx.obj.state.save)

"""
Convert an existing, old-style etc/control file to a new YAML control file.
"""
@trees.command('convert', help = 'convert an old-style control file to YAML')
@click.argument('filename', type = click.Path(exists = True, dir_okay = False,
                                              path_type = pathlib.Path),
                default = pathlib.Path('../etc/control'))
@click.pass_obj
def trees_convert(obj, filename):
    print(f'converting trees file: {filename}')

    obj.control = nut.ControlFile(filename, obj.repo)

    remotes = { remote.name: remote for remote in obj.repo.remotes }
    actions = []

    for action in obj.control:
        entry = {
            'action': action.type,
            'name': action.name
        }

        if action.type == 'switch':
            if action.base:
                entry['base'] = action.base

        if hasattr(action, 'url') and action.url:
            if action.name in remotes:
                entry['url'] = list(remotes[action.name].urls)[0]
            else:
                entry['url'] = 'linux-next'

        if action.type == 'merge':
            entry['branch'] = action.branch

        if hasattr(action, 'contacts') and action.contacts:
            entry['contacts'] = action.contacts

        actions.append(entry)

    with open(filename.parent / 'control.yml', 'w') as fobj:
        yaml.dump(actions, fobj, Dumper = nut.ActionDumper, sort_keys = False,
                  allow_unicode = True)

@trees.command('list')
@click.pass_obj
def trees_list(obj):
    print('listing trees')

    obj.control = nut.ControlFile.load(obj.repo)

    for action in obj.control:
        print(action)

@trees.command('fetch')
@click.pass_obj
def trees_fetch(obj):
    print('fetching trees')

    obj.control = nut.ControlFile.load(obj.repo)

    for action in obj.control:
        action.fetch(obj.repo, obj.state)

@trees.command('check')
@click.pass_obj
def trees_check(obj):
    print('checking trees')

    obj.control = nut.ControlFile.load(obj.repo)

    for action in obj.control:
        if isinstance(action, ActionMerge):
            if action.is_dirty(obj.state):
                action.check(obj.repo, obj.state)

@cli.command('merge')
@click.pass_context
def merge(ctx):
    print('merging trees')

    ctx.obj.control = ControlFile('../etc/control', ctx.obj.repo)
    ctx.obj.state = StateCache()

    for action in obj.control:
        action.merge(obj.repo, obj.state)

@cli.command('contacts')
@click.argument('tree')
@click.pass_context
def contacts(ctx, tree):
    ctx.obj.control = ControlFile('../etc/control', ctx.obj.repo)
    ctx.obj.state = StateCache()

    for action in ctx.obj.control:
        if action.name == tree:
            if not hasattr(action, 'contacts'):
                click.echo(f'ERROR: no contacts for tree {tree}')
                break

            for contact in action.contacts:
                print(contact)

            break

@cli.command('build')
@click.option('--stage', type = str, default = 'default')
@click.pass_context
def build(ctx, stage):
    builders = []

    with open('builds.yml', 'r') as fobj:
        data = yaml.safe_load(fobj)

    for key, value in data['builders']['pool'].items():
        print(f'Host: {key}')
        builder = Builder(key)
        builders.append(builder)

    for key, batches in data['stages'].items():
        if key == stage:
            for batch in batches:
                print(f'building {batch['arch']}')
                batch = Batch(batch)
                batch.build(builders[0])

            break

def main():
    cli(obj = NutContext())

if __name__ == '__main__':
    main()
