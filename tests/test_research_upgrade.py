"""Source installer tests; all paths/accounts here are artificial sentinels."""
import importlib.util
import json
from pathlib import Path

import pytest

spec=importlib.util.spec_from_file_location('source_upgrade',Path(__file__).parents[1]/'scripts/apply_research_upgrade.py')
upgrade=importlib.util.module_from_spec(spec)
spec.loader.exec_module(upgrade)

@pytest.fixture
def package(tmp_path):
    src,tmp=tmp_path/'package',tmp_path/'target'
    src.mkdir();tmp.mkdir()
    (tmp/'pyproject.toml').write_text('SYNTHETIC')
    (src/'a.py').write_text('NEW');(tmp/'a.py').write_text('OLD')
    (src/'new.py').write_text('NEW FILE')
    (tmp/'runtime').mkdir();(tmp/'runtime/account.sqlite').write_bytes(b'SYNTHETIC_KEEP')
    (tmp/'.env').write_text('SYNTHETIC_ENV_KEEP')
    entries=[dict(path=p,before_sha256=upgrade.sha(tmp/p),after_sha256=upgrade.sha(src/p)) for p in ('a.py','new.py')]
    (src/'upgrade-manifest.json').write_text(json.dumps(dict(files=entries)))
    return src,tmp


def test_dry_run_apply_idempotency_and_verified_rollback(package):
    src,target=package
    assert upgrade.upgrade(src,target)['status']=='dry_run'
    assert not (target/'.research-upgrade-backups').exists()
    result=upgrade.upgrade(src,target,True)
    assert (target/'a.py').read_text()=='NEW'
    assert upgrade.upgrade(src,target,True)['status']=='already_installed_identical'
    assert upgrade.rollback(target,result['backup_id'])['status']=='rollback_dry_run'
    upgrade.rollback(target,result['backup_id'],True)
    assert (target/'a.py').read_text()=='OLD' and not (target/'new.py').exists()
    assert (target/'runtime/account.sqlite').read_bytes()==b'SYNTHETIC_KEEP'
    assert (target/'.env').read_text()=='SYNTHETIC_ENV_KEEP'


def test_source_conflict_aborts_before_any_write(package):
    src,target=package
    (target/'a.py').write_text('LOCAL_EDIT')
    with pytest.raises(ValueError,match='local_source_conflict'): upgrade.upgrade(src,target,True)
    assert not (target/'new.py').exists() and not (target/'.research-upgrade-backups').exists()


def test_rollback_preserves_later_user_edit(package):
    src,target=package
    result=upgrade.upgrade(src,target,True)
    (target/'new.py').write_text('USER_EDIT')
    with pytest.raises(ValueError,match='rollback_local_edit_conflict'):
        upgrade.rollback(target,result['backup_id'],True)
    assert (target/'a.py').read_text()=='NEW' and (target/'new.py').read_text()=='USER_EDIT'


@pytest.mark.parametrize('path',['runtime/account.sqlite','.env','../escape','/absolute','src/../../runtime/f','src\\hidden.py'])
def test_private_or_escape_paths_rejected(tmp_path,path):
    with pytest.raises(ValueError): upgrade.safe(tmp_path,path)
