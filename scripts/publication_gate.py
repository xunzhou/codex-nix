#!/usr/bin/env python3
"""Fail-closed checks for Git objects and exact public API payloads."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import unicodedata


class Blocked(ValueError):
    pass


def require(ok, category, location):
    if not ok:
        raise Blocked(f'{category}: {location}')


def run(*args, data=None, env=None):
    result = subprocess.run(args, input=data, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    require(result.returncode == 0, 'command-failed', 'publication')
    return result.stdout


def git(*args, data=None):
    # Replacement objects and shallow boundaries must not hide outgoing history.
    return run('git', '--no-replace-objects', *args, data=data,
               env={**os.environ, 'GIT_NO_LAZY_FETCH': '1'})


def digest(data):
    return hashlib.sha256(data).hexdigest()


def scan(data, location, limit=None):
    require(len(data) <= (limit or 8 * 1024 * 1024), 'size-limit', location)
    try:
        text = data.decode('utf-8')
    except UnicodeError:
        raise Blocked(f'non-utf8: {location}') from None
    require(not any(unicodedata.category(c) in ('Cc', 'Cf') and c not in '\n\r\t'
                    for c in text), 'binary-or-control', location)
    emails = re.findall(r'[\w.!#$%&*+/=?^{|}~\[\]-]+@[\w.-]+\.[A-Za-z]{2,}', text)
    require(all(re.fullmatch(r'[A-Za-z0-9+_\[\].-]+@users\.noreply\.github\.com', e)
                for e in emails), 'email', location)
    patterns = [r'gh[pousr]_[A-Za-z0-9]{20,}', r'github_pat_[A-Za-z0-9_]{20,}',
                r'AKIA[A-Z0-9]{16}', r'-----BEGIN [A-Z ]*PRIVATE KEY-----',
                r'(?im)^\s*(?:user|assistant|system|developer)\s*:\s*(?:[A-Za-z]|$)',
                r'(?i)<(?:transcript_evidence|codex_delegation)>',
                r'(?i)revision\s+\d+\s+of\s+at\s+most\s+\d+',
                r'(?i)(?:approval|retry)\s+budget',
                r'(?i)instructions\s+(?:intended|meant)\s+to\s+steer']
    require(not any(re.search(p, text) for p in patterns), 'private-content', location)
    # Optional private deny rules stay in Git config, never in tracked policy files.
    rules = subprocess.run(['git', 'config', '--get-all', 'publication.denyPattern'],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    require(rules.returncode in (0, 1), 'policy-read', location)
    try:
        require(not any(re.search(p, text) for p in rules.stdout.decode().splitlines()),
                'private-pattern', location)
    except (UnicodeError, re.error):
        raise Blocked(f'invalid-policy: {location}') from None
    return text


def identities():
    raw = git('config', '--get-all', 'publication.identity').decode().splitlines()
    require(bool(raw), 'identity-policy', 'config')
    for item in raw:
        require(re.fullmatch(r'[^<>\n]+ <[A-Za-z0-9+_\[\].-]+@users\.noreply\.github\.com>', item),
                'identity-policy', 'config')
    return set(raw)


def oid(value):
    require(bool(re.fullmatch(r'[a-f0-9]{40}|[a-f0-9]{64}', value)), 'object-id', 'ref')
    return value


def advertised(remote):
    result = {}
    for line in git('ls-remote', '--refs', '--', remote).decode().splitlines():
        value, ref = line.split('\t')
        result[ref] = oid(value)
    return result


def check_updates(updates, baseline, allowed):
    require(git('rev-parse', '--is-shallow-repository').strip() == b'false', 'shallow', 'repository')
    require(not git('for-each-ref', 'refs/replace'), 'replacement-objects', 'repository')
    grafts = Path(os.fsdecode(git('rev-parse', '--git-path', 'info/grafts').strip()))
    require(not grafts.exists(), 'grafts', 'repository')
    tips = []
    for index, (new, ref) in enumerate(updates):
        scan(ref.encode(), f'ref-{index}')
        require(ref.startswith(('refs/heads/', 'refs/tags/')), 'unsupported-ref', f'ref-{index}')
        oid(new)
        if set(new) == {'0'}:
            continue  # Deletion transmits no new object; the ref name was checked.
        kind = git('cat-file', '-t', new).strip()
        require(kind == b'commit', 'unsupported-tag-or-object', f'ref-{index}')
        tips.append(new)
    if not tips:
        return []
    # Verify the entire outgoing closure before exclusions can hide missing history.
    git('rev-list', '--objects', '--missing=error', '--stdin',
        data=('\n'.join(tips) + '\n').encode())
    # Unknown advertised refs are omitted, never replaced by tracking refs.
    # An omitted exclusion causes more scanning, not less.
    exclusions = []
    for value in sorted(set(baseline.values())):
        oid(value)
        kind = git('cat-file', '--batch-check=%(objecttype)', data=(value + '\n').encode()).strip()
        if kind == b'commit':
            exclusions.append('^' + value)
        # Annotated tags are not needed as exclusions; omit them conservatively.
    revisions = '\n'.join(tips + exclusions) + '\n'
    commits = git('rev-list', '--stdin', data=revisions.encode()).decode().splitlines()
    for commit in commits:
        raw = git('cat-file', 'commit', commit)
        scan(raw, 'commit-' + commit)
        headers, message = commit_parts(raw, commit)
        for field in (b'author', b'committer'):
            lines = [s for s in headers.splitlines() if s.startswith(field + b' ')]
            require(len(lines) == 1, 'identity-header', commit)
            match = re.fullmatch(rb'\w+ (.+ <[^<>]+>) [0-9]+ [+-][0-9]{4}', lines[0])
            require(match is not None and match[1].decode() in allowed, 'identity', commit)
        scan(message, 'message-' + commit, 2000)
        # Walk each newly published tree. This includes names and modes inherited
        # from parents, but only changed/new blobs are scanned below.
        tree = git('ls-tree', '-rz', '--full-tree', commit)
        for number, entry in enumerate(tree.split(b'\0')):
            if not entry:
                continue
            metadata, name = entry.split(b'\t', 1)
            scan(name, f'path-{commit}-{number}')
            mode, kind, value = metadata.split()
            require(kind == b'blob' and mode in (b'100644', b'100755', b'120000'),
                    'unsupported-tree-entry', f'path-{commit}-{number}')
    # --objects visits EVERY new blob version, including blobs later deleted.
    objects = git('rev-list', '--objects', '--no-object-names', '--stdin',
                  data=revisions.encode()).decode().splitlines()
    for value in objects:
        kind = git('cat-file', '-t', value).strip()
        if kind == b'blob':
            size = int(git('cat-file', '-s', value))
            require(size <= 8 * 1024 * 1024, 'size-limit', 'blob-' + value)
            scan(git('cat-file', 'blob', value), 'blob-' + value)
        else:
            require(kind in (b'commit', b'tree'), 'unsupported-object', value)
    return commits


def commit_parts(raw, location):
    parts = raw.split(b'\n\n', 1)
    require(len(parts) == 2, 'commit-structure', location)
    headers, message = parts
    lines = headers.split(b'\n')
    require(bool(lines) and re.fullmatch(rb'tree [a-f0-9]{40}|tree [a-f0-9]{64}', lines[0]),
            'commit-structure', location)
    index = 1
    while index < len(lines) and lines[index].startswith(b'parent '):
        require(re.fullmatch(rb'parent [a-f0-9]{40}|parent [a-f0-9]{64}', lines[index]),
                'commit-structure', location)
        index += 1
    require(len(lines) == index + 2 and lines[index].startswith(b'author ')
            and lines[index + 1].startswith(b'committer '), 'unsupported-commit-headers', location)
    return headers, message


def decode_payload(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'duplicate-json-key', 'payload')
            result[key] = value
        return result

    try:
        text = raw.decode('utf-8') if isinstance(raw, bytes) else raw
        return json.loads(text, object_pairs_hook=unique,
                          parse_constant=lambda _: require(False, 'json-constant', 'payload'))
    except (UnicodeError, json.JSONDecodeError):
        raise Blocked('invalid-json: payload') from None


def receipt(kind, value):
    # No content, ref names, or identities in diagnostics/receipts.
    print(json.dumps({'gate': 1, 'kind': kind, 'sha256': digest(value)}, sort_keys=True))


def pre_push(remote, stream):
    updates = []
    for line in stream.splitlines():
        parts = line.split()
        require(len(parts) == 4, 'hook-input', 'stdin')
        _, new, ref, old = parts
        oid(old)
        updates.append((new, ref))
    baseline = advertised(remote)
    commits = check_updates(updates, baseline, identities())
    receipt('git', json.dumps({'updates': updates, 'baseline': baseline, 'commits': commits},
                              sort_keys=True).encode())


def payload_check(data, raw=None, route="release"):
    require(isinstance(data, dict), 'payload-schema', 'payload')
    fields = {
        'release': {'body', 'name', 'draft', 'tag_name', 'target_commitish',
                    'make_latest', 'prerelease'},
        'pr': {'title', 'body', 'head', 'base', 'draft'},
        'comment': {'body'},
    }
    require(route in fields, 'unsupported-route', 'payload')
    require(set(data) <= fields[route], 'payload-schema', 'keys')
    for key, value in data.items():
        location = 'field-' + key
        if key in ('draft', 'prerelease'):
            require(type(value) is bool, 'payload-type', location)
        else:
            require(isinstance(value, str), 'payload-type', location)
            if key == 'make_latest':
                require(value in ('true', 'false', 'legacy'), 'payload-value', location)
            scan(value.encode(), location, 120 if key in ('title', 'name') else 2000)
    if raw is None:
        raw = json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode()
    require(decode_payload(raw) == data, 'payload-mismatch', 'payload')
    receipt('text', raw)
    return raw


def api_publish(kind, number, title, body, head, expected_head=None):
    # Read once; the very same serialized bytes are scanned and sent on stdin.
    data = {'body': Path(body).read_bytes().decode('utf-8')}
    if kind == 'pr':
        require(head is not None and title is not None, 'required-field', 'pr')
        require(expected_head is not None, 'required-field', 'expected-head')
        oid(expected_head)
        require(advertised('https://github.com/xunzhou/codex-nix.git').get('refs/heads/' + head) == expected_head,
                'head-mismatch', 'pr')
        data.update(title=Path(title).read_bytes().decode('utf-8'),
                    head=head, base='main', draft=True)
        endpoint = 'pulls'
    elif kind == 'comment':
        require(number is not None and number > 0, 'required-field', 'comment')
        endpoint = f'issues/{number}/comments'
    else:
        require(number is not None and number > 0 and title is not None,
                'required-field', 'release')
        data['name'] = Path(title).read_bytes().decode('utf-8')
        endpoint = f'releases/{number}'
    payload = payload_check(data, route=kind)
    run('gh', 'api', '--hostname', 'github.com', '--method',
        'PATCH' if kind == 'release' else 'POST',
        'repos/xunzhou/codex-nix/' + endpoint, '--input', '-', data=payload)
    receipt('submitted-text', payload)
    if kind == 'pr':
        receipt('pr-head-and-text', expected_head.encode() + b'\n' + payload)


def install():
    # Repository-local only; preserve unrelated hooks rather than overwrite them.
    root = Path(os.fsdecode(git('rev-parse', '--show-toplevel').strip()))
    existing = subprocess.run(['git', 'config', '--get', 'core.hooksPath'], capture_output=True)
    require(existing.returncode == 1 or existing.stdout.strip() == b'.githooks',
            'existing-hooks', 'config')
    hooks = Path(os.fsdecode(git('rev-parse', '--git-path', 'hooks/pre-push').strip()))
    require(not hooks.exists(), 'existing-hooks', 'pre-push')
    pairs = {(os.environ.get('GIT_' + role + '_NAME', ''),
              os.environ.get('GIT_' + role + '_EMAIL', '')) for role in ('AUTHOR', 'COMMITTER')}
    require(all(name and re.fullmatch(r'[A-Za-z0-9+_\[\].-]+@users\.noreply\.github\.com', email)
                and not any(c in name for c in '<>\r\n') for name, email in pairs),
            'approved-noreply-environment-required', 'install')
    git('config', '--local', '--replace-all', 'publication.identity',
        'github-actions[bot] <41898282+github-actions[bot]@users.noreply.github.com>')
    for name, email in sorted(pairs):
        git('config', '--local', '--add', 'publication.identity', f'{name} <{email}>')
    require((root / '.githooks/pre-push').is_file(), 'missing-hook', 'install')
    git('config', '--local', 'core.hooksPath', '.githooks')
    print('Installed repository publication hook.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('install')
    sub.add_parser('verify')
    bundle = sub.add_parser('bundle')
    bundle.add_argument('--output', required=True)
    check = sub.add_parser('check-head')
    check.add_argument('--remote', default='origin')
    hook = sub.add_parser('pre-push')
    hook.add_argument('remote')
    hook.add_argument('url')
    api = sub.add_parser('publish')
    api.add_argument('kind', choices=('pr', 'comment', 'release'))
    api.add_argument('--number', type=int)
    api.add_argument('--title')
    api.add_argument('--body', required=True)
    api.add_argument('--head')
    api.add_argument('--expected-head')
    payload = sub.add_parser('payload')
    args = parser.parse_args()
    try:
        if args.command == 'install':
            install()
        elif args.command == 'verify':
            identities()
            require(git('config', '--get', 'core.hooksPath').strip() == b'.githooks',
                    'hook-not-installed', 'config')
            require(os.access('.githooks/pre-push', os.X_OK), 'hook-not-executable', 'hook')
            print('Repository hook configuration verified.')
        elif args.command == 'bundle':
            head = git('rev-parse', 'HEAD').decode().strip()
            pre_push('origin', f'HEAD {head} refs/heads/main ' + '0' * len(head))
            git('bundle', 'create', args.output, 'HEAD')
            listed = git('bundle', 'list-heads', args.output).decode().splitlines()
            require(listed == [head + ' HEAD'], 'bundle-head-race', 'bundle')
            with open(args.output, 'rb') as stream:
                print(json.dumps({'gate': 1, 'kind': 'source-bundle', 'commit': head,
                                  'sha256': hashlib.file_digest(stream, 'sha256').hexdigest()}))
        elif args.command == 'check-head':
            head = git('rev-parse', 'HEAD').decode().strip()
            pre_push(args.remote, f'HEAD {head} refs/heads/main ' + '0' * len(head))
        elif args.command == 'pre-push':
            pre_push(args.url, sys.stdin.read())
        elif args.command == 'payload':
            raw = sys.stdin.buffer.read()
            payload_check(decode_payload(raw), raw)
        else:
            api_publish(args.kind, args.number, args.title, args.body, args.head, args.expected_head)
    except (Blocked, OSError, ValueError, KeyError) as error:
        print(str(error) if isinstance(error, Blocked) else 'scan-or-publication-error: publication',
              file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
