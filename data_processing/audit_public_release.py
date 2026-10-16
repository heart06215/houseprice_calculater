"""Reject high-confidence credentials in Git files and published input snapshots.

Reports contain only paths, rules and line numbers. This guard supplements human
privacy review; it does not prove arbitrary source data contains no personal data.
"""
import argparse
import json
import re
import subprocess
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    'private-key': rb'-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY',
    'github-token': rb'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,})',
    'cloud-access-key': rb'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|\bAKID[A-Za-z0-9]{28,40}\b',
    'api-token': rb'\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{30,}',
    'slack-token': rb'\bxox[baprs]-[A-Za-z0-9-]{20,}',
    'credential-in-url': rb'https?://[^\s/<>\'"]{1,200}:[^\s/@<>\'"]{1,200}@',
    'jwt': rb'\beyJ[A-Za-z0-9_-]{12,}\.eyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{20,}',
    'personal-home-path': (rb'(?i)(?:[A-Z]:[/\\]+Users[/\\]+[^/\\\s\'"]+|(?<![\w:/])/Us'
                           rb'ers/[^/\s\'"]+)'),
}


def inspect_content(name, content):
    return [dict(path=name, rule=rule, line=content[:match.start()].count(b'\n') + 1)
            for rule, pattern in PATTERNS.items() for match in re.finditer(pattern, content)]


def audit(root=ROOT):
    root = Path(root).resolve()
    result = subprocess.run(['git', 'ls-files', '-co', '--exclude-standard', '-z'],
                            cwd=root, capture_output=True, check=True)
    names = sorted(set(result.stdout.decode('utf-8').split('\0')) - {''})
    findings, files, members = [], 0, 0
    for name in names:
        path = root / name
        if not path.is_file():
            continue
        if path.is_symlink() or root not in path.resolve().parents:
            findings.append(dict(path=name, rule='linked-or-outside-file'))
            continue
        relative = PurePosixPath(name)
        filename = relative.name.lower()
        example = filename in ('.env.example', '.env.sample')
        if ((filename == '.env' or filename.startswith('.env.')) and not example
                or filename.endswith(('.pem', '.key', '.p12', '.pfx'))
                or filename in ('id_rsa', 'id_ed25519')
                or filename.startswith(('cookies', 'storage_state'))):
            findings.append(dict(path=name, rule='credential-or-session-file'))
        files += 1
        if path.suffix.lower() == '.zip':
            with zipfile.ZipFile(path) as archive:
                for item in archive.infolist():
                    member = PurePosixPath(item.filename)
                    if member.is_absolute() or '..' in member.parts or ':' in item.filename:
                        findings.append(dict(path=name, rule='unsafe-archive-path'))
                        continue
                    members += 1
                    findings.extend(inspect_content(name + '!' + item.filename, archive.read(item)))
        elif path.suffix.lower() not in ('.png', '.jpg', '.jpeg', '.webp', '.woff2', '.ttf'):
            findings.extend(inspect_content(name, path.read_bytes()))
    return dict(status='failed' if findings else 'passed', files_scanned=files,
                archive_members_scanned=members, findings=findings)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    args = parser.parse_args()
    report = audit(args.root)
    print(json.dumps(report, ensure_ascii=False))
    raise SystemExit(1 if report['findings'] else 0)
