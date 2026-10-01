"""Package only committed source; exclude runtime data and local credentials."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import zipfile

root = Path(__file__).resolve().parents[1]
def git(*args):
    return subprocess.check_output(['git', '-C', str(root), *args])

if git('status', '--porcelain').strip():
    raise SystemExit('Commit reviewed changes before packaging')
commit = git('rev-parse', 'HEAD').decode().strip()
release = 'clearmeeting-server-v2.1.0-daily-message-r2'
output = Path(sys.argv[1]).resolve()
output.mkdir(parents=True, exist_ok=True)
payload = git('archive', '--format=zip', 'HEAD')
with zipfile.ZipFile(io.BytesIO(payload)) as archive:
    files = {n: archive.read(n) for n in archive.namelist() if not n.endswith('/')}
manifest = {'schema': 1, 'release_id': release, 'server_version': '2.1.0',
            'api_contract': 'luoye-device-api/2', 'source_commit': commit,
            'persistent_data_included': False, 'deploy_env_included': False,
            'files': [{'path': n, 'bytes': len(b), 'sha256': hashlib.sha256(b).hexdigest()}
                      for n, b in sorted(files.items())]}
path = output / (release + '-source.zip')
with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
    for name, content in files.items():
        archive.writestr(release + '/' + name, content)
    archive.writestr(release + '/RELEASE-MANIFEST.json', json.dumps(manifest, ensure_ascii=False, indent=2))
digest = hashlib.sha256(path.read_bytes()).hexdigest()
(output / 'SHA256SUMS.txt').write_text(digest + '  ' + path.name + '\n', encoding='utf-8')
print(path)
print('SHA256=' + digest)
