"""A reused source marker must not let altered SDK code pass the pin."""
import hashlib
import importlib.util
import json
from pathlib import Path
import zipfile
import pytest

spec=importlib.util.spec_from_file_location('verify_dsh_source',Path(__file__).resolve().parents[1]/'scripts/verify-dsh-source.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


def test_source_archive_and_reused_tree_are_both_verified(tmp_path):
    ref='a'*40
    archive=tmp_path/'source.zip'
    with zipfile.ZipFile(archive,'w') as z:z.writestr(f'deepseek-harness-{ref}/python/sdk/client.py','pinned code')
    lock=tmp_path/'lock.json';lock.write_text(json.dumps({'deepseek_harness':{'ref':ref,'source_archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest()}}))
    source=tmp_path/'source';(source/'python/sdk').mkdir(parents=True)
    target=source/'python/sdk/client.py';target.write_text('pinned code')
    (source/'.v2-pinned-commit').write_text(ref)
    module.verify(lock,archive,source)
    target.write_text('altered code')
    with pytest.raises(ValueError,match='source differs'):module.verify(lock,archive,source)
    target.write_text('pinned code');(source/'python/sdk/extra.py').write_text('injected')
    with pytest.raises(ValueError,match='Unverified'):module.verify(lock,archive,source)
    archive.write_bytes(b'invalid download')
    with pytest.raises(ValueError,match='SHA256'):module.verify(lock,archive,source)
