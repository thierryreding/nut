#!/usr/bin/env python3

import click, git, json, os, os.path, paramiko, pathlib, re, yaml

def parse_trailers(commit):
    pattern = re.compile(r'^([a-zA-Z0-9-]+):[ \t]*(.*)$')
    p = commit.message.strip().split('\n\n', maxsplit = 1)[-1]
    trailers = {}

    for line in p.splitlines():
        match = pattern.match(line.strip())
        if match:
            key, value = match.groups()
            key = key.strip()
            value = value.strip()

            if key not in trailers:
                trailers[key] = []

            trailers[key].append(value)

    return trailers

class CommitIssue:
    def __init__(self, type, message = None):
        self.type = type
        self.message = message
        self.commits = []

class SignoffCommitIssue(CommitIssue):
    def __init__(self, target):
        super().__init__('missing-signoff-')

    def __str__(self):
        return f'{self} missing a Signed-off-by from {self} committers'

class TreeReport:
    def __init__(self, name):
        self.name = name
        self.issues = {}

    def add_issue(self, type, commit):
        if not type in self.issues:
            self.issues[type] = CommitIssue(type, message)

        self.issues.append()

    def check_signoff(self, repo, commit, trailers):
        found = {
            'committer': False,
            'author': False
        }
        messages = []

        if 'Signed-off-by' not in trailers:
            # Linus' releases are a special case where no signed-off-by is needed
            match = re.match(r'Linux ((\d+\.)+\d+)', commit.message)
            if match:
                return messages

            click.echo(f'ERROR: no signed-off-by trailers: {trailers}')
            click.echo(commit.message)

        for value in trailers['Signed-off-by']:
            match = re.match(r'^(.*)\s+<(.*)>$', value)
            if match:
                name, email = match.groups()
                actor = git.Actor(name, email)

                if actor == commit.committer:
                    found['committer'] = True

                if actor == commit.author:
                    found['author'] = True
            else:
                messages.append((commit, 'malformed', value))

        if not found['committer']:
            messages.append((commit, 'missing-signoff-committer', commit.committer))

        if not found['author']:
            messages.append((commit, 'missing-signoff-author', commit.author))

        return messages

    def check_fixes(repo, commit, trailers):
        return []

    def check_trailers(repo, commit):
        trailers = parse_trailers(commit)
        messages = []

        messages += check_signoff(repo, commit, trailers)
        messages += check_fixes(repo, commit, trailers)

        return messages

    def check_files(repo, commit):
        SUSPICIOUS_PATTERNS = [ '*.rej', '*.orig', '.config*', '*.swp' ]
        messages = []

        for filename, info in commit.stats.files.items():
            path = pathlib.Path(filename)

            for pattern in SUSPICIOUS_PATTERNS:
                if path.match(pattern):
                    messages.append()

        return []

    def generate(self):
        print(f'found {len(self.issues)} issues:')

        for issue in self.issues:
            print(f'  {issue}')

class Action:
    def __init__(self, type, name):
        self.type = type
        self.name = name

    def __str__(self):
        return f'{self.type}: {self.name}'

    def fetch(self, repo, state):
        print(f'fetching {self.name}...')

    def merge(self, repo, state):
        print(f'merging {self.name}...')

class ActionMerge(Action):
    def __init__(self, name, url, branch, contacts, flags, remote = None):
        super().__init__('merge', name)

        self.url = url
        self.branch = branch
        self.flags = flags
        self.contacts = []
        self.remote = remote

        if contacts:
            for contact in contacts.split(','):
                # special case found in the control file for merges from
                # previously created linux-next branches
                if contact == '<>':
                    continue

                self.contacts.append(contact.strip())

    def __str__(self):
        return f'{super().__str__()} -> {self.branch}: {self.url}'

    def is_dirty(self, state):
        return state.is_tree_dirty(self.name, self.branch)

    def fetch(self, repo, state):
        if self.remote:
            print(f'fetching branch {self.branch} from {self.name}')
            old = repo.commit(f'{self.name}/{self.branch}').hexsha

            try:
                infos = self.remote.fetch(self.branch, verbose = True)
            except git.GitCommandError as e:
                click.echo(f'❌failed to fetch {self.name}/{self.branch}: {e}')
                return

            for info in self.remote.fetch(self.branch):
                new = info.commit.hexsha

                if new != old:
                    print(f'{self.name}/{self.branch} updated: {old}..{new}')
                    state.update_tree(self.name, self.branch, status = 'new', new = new)

    def check(self, repo, state):
        print(f'checking tree {self.name}/{self.branch}...')

        old, new = state.get_tree_shas(self.name, self.branch)

        try:
            commits = list(repo.iter_commits(f'{old}..{new}'))
        except git.GitCommandError:
            print(f'WARNING: {self.name}/{self.branch} was rebased, checking all commits since common ancestor')
            commits = list(repo.iter_commits(f'{old}...{new}'))

        report = TreeReport(self.name)

        for commit in commits:
            # skip merge commits
            if len(commit.parents) > 1:
                continue

            print(f'  {commit}')

            check_trailers(repo, commit, report)
            check_files(repo, commit, report)

            report.generate()

    def merge(self, repo, state):
        print(f'merging branch {self.branch}')

class ActionSwitch(Action):
    def __init__(self, name, target):
        super().__init__('switch', name)

        self.target = target

    def __str__(self):
        return f'{super().__str__()} -> {self.target}'

    def fetch(self, repo, state):
        pass

    def merge(self, repo, state):
        print(f'switching to {self.branch}')

class ActionBranch(Action):
    def __init__(self, name):
        super().__init__('branch', name)

    def fetch(self, repo, state):
        pass

    def merge(self, repo, state):
        print(f'creating branch {self.name}')

class ControlFile:
    def __init__(self, filename, repo = None):
        self.actions = []

        if not repo:
            repo = git.Repo('.')

        with open(filename, 'r') as fobj:
            for line in fobj.readlines():
                # skip comments
                if line[0] == '#':
                    continue

                try:
                    contacts, kind, params = line.strip().split('\t', maxsplit = 2)
                except Exception as e:
                    print(f'ERROR: failed to parse: "{line}"')
                    raise e

                if contacts == '-':
                    contacts = None

                if kind == 'git':
                    name, url, branch, flags = params.split('\t')

                    try:
                        remote = repo.remote(name)
                    except:
                        remote = None

                    action = ActionMerge(name, url, branch, contacts, flags, remote)
                elif kind == 'switch':
                    branch, target, *_ = params.split('\t') + [None]
                    action = ActionSwitch(branch, target)
                elif kind == 'branch':
                    action = ActionBranch(params)
                else:
                    print(f'ERROR: failed to parse action: {kind}')
                    continue

                self.actions.append(action)

    def __iter__(self):
        return iter(self.actions)

    def __len__(self):
        return len(self.actions)

class StateCache:
    def __init__(self, path = pathlib.Path.cwd().parent / 'next.json'):
        self.path = path
        self.trees = {}
        self.merge = {}
        self._load()

    def _load_file(self, filename):
        if filename.exists():
            with open(filename, 'r') as fobj:
                return json.load(fobj)

        return {}

    def _save_file(self, filename, data):
        temp = filename.with_suffix(filename.suffix + '.tmp')

        with open(temp, 'w') as fobj:
            json.dump(data, fobj, indent = 4)

        temp.replace(filename)

    def _load(self):
        data = self._load_file(self.path)

        if 'trees' in data:
            self.trees = data['trees']
        else:
            self.trees = {}

        if 'merge' in data:
            self.merge = data['merge']
        else:
            self.merge = {}

    def save(self):
        data = {
            'trees': self.trees,
            'merge': self.merge
        }

        self._save_file(self.path, data)

    def update_tree(self, tree, branch, status = None, old = None, new = None):
        if not tree in self.trees:
            self.trees[tree] = {}

        if not branch in self.trees[tree]:
            if status is None:
                status = 'new'

            if old is None:
                old = '0000000000000000000000000000000000000000'

            if new is None:
                new = '0000000000000000000000000000000000000000'

            self.trees[tree][branch] = {
                "status": status,
                "old": old,
                "new": new
            }
        else:
            if status is not None:
                self.trees[tree][branch]['status'] = status

            if old is not None:
                self.trees[tree][branch]['old'] = old

            if new is not None:
                self.trees[tree][branch]['new'] = new

    def is_tree_dirty(self, tree, branch):
        if tree in self.trees and branch in self.trees[tree]:
            return self.trees[tree][branch]['old'] != self.trees[tree][branch]['new']

        return False

    def get_tree_shas(self, tree, branch):
        if not tree in self.trees or not branch in self.trees[tree]:
            return None, None

        return self.trees[tree][branch]['old'], self.trees[tree][branch]['new']

    def merge_reset(self):
        self.merge = {}

class Builder:
    def __init__(self, host, user = None):
        self.host = host
        self.user = user

class Build:
    def __init__(self, arch, values):
        self.arch = arch
        self.name = values['name']
        self.type = values['type']

        if 'config' in values:
            self.config = values['config']
        else:
            self.config = {}

    def run(self, ssh):
        hostname = ssh.get_transport().hostname

        stdin, stdout, stderr = ssh.exec_command(f'echo building {self.arch} {self.name} on $(hostname)')

        for line in stdout:
            click.echo(line)

class Batch:
    def __init__(self, values):
        self.arch = values['arch']
        self.builds = []

        for build in values['builds']:
            build = Build(self.arch, build)
            self.builds.append(build)

    def build(self, builder):
        ssh = paramiko.SSHClient()
        ssh.load_system_host_keys(filename = str(pathlib.Path('~/.ssh/known_hosts').expanduser()))
        ssh.connect(builder.host, username = builder.user,
                    key_filename = str(pathlib.Path('~/.ssh/id_kbuild').expanduser()),
                    look_for_keys = False, allow_agent = True)

        for build in self.builds:
            build.run(ssh)

class BuildList:
    def __init__(self):
        self.builds = []

class ActionDumper(yaml.SafeDumper):
    def write_line_break(self, data = None):
        super().write_line_break(data)

        if len(self.indents) == 1:
            super().write_line_break()

class NutContext:
    def __init__(self):
        self.topdir = pathlib.Path.cwd()
        self.repo = None

@click.group
@click.pass_context
def nut(ctx):
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

@nut.group
@click.pass_context
def trees(ctx):
    ctx.obj.control = ControlFile('../etc/control', ctx.obj.repo)
    ctx.obj.state = StateCache()

    ctx.call_on_close(ctx.obj.state.save)

@trees.command('convert')
@click.pass_obj
def trees_convert(obj):
    print('converting trees file')

    remotes = { remote.name: remote for remote in obj.repo.remotes }
    actions = []

    for action in obj.control:
        entry = {
            'action': action.type,
            'name': action.name
        }

        if hasattr(action, 'url') and action.url:
            if action.name in remotes:
                entry['url'] = list(remotes[action.name].urls)[0]
            else:
                entry['url'] = 'linux-next'

        if hasattr(action, 'contacts') and action.contacts:
            entry['contacts'] = action.contacts

        actions.append(entry)

    with open('control.yml', 'w') as fobj:
        yaml.dump(actions, fobj, Dumper = ActionDumper, sort_keys = False)

@trees.command('list')
@click.pass_obj
def trees_list(obj):
    print('listing trees')

    for action in obj.control:
        print(action)

@trees.command('fetch')
@click.pass_obj
def trees_fetch(obj):
    print('fetching trees')

    for action in obj.control:
        action.fetch(obj.repo, obj.state)

@trees.command('check')
@click.pass_obj
def trees_check(obj):
    print('checking trees')

    for action in obj.control:
        if isinstance(action, ActionMerge):
            if action.is_dirty(obj.state):
                action.check(obj.repo, obj.state)

@nut.command('merge')
@click.pass_context
def merge(ctx):
    print('merging trees')

    ctx.obj.control = ControlFile('../etc/control', ctx.obj.repo)
    ctx.obj.state = StateCache()

    for action in obj.control:
        action.merge(obj.repo, obj.state)

@nut.command('contacts')
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

@nut.command('build')
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
    nut(obj = NutContext())

if __name__ == '__main__':
    main()
