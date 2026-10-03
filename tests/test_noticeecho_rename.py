"""The new product identity must not strand an existing database or settings."""
import os
from pathlib import Path
import warnings

import pytest

from app import config
import importlib.util
_spec = importlib.util.spec_from_file_location("noticeecho_launcher", Path(__file__).parents[1]/"packaging/launcher.py")
launcher = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(launcher)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for prefix in ('NOTICE_ECHO','WX_ECHO','MESH_WX'):
        for suffix in ('HOST','PORT','DB'):
            monkeypatch.delenv(prefix+'_'+suffix,raising=False)
    monkeypatch.setattr(config.os.path,'isdir',lambda p: False)


def test_new_environment_has_priority_over_all_old_aliases(monkeypatch,tmp_path):
    for prefix,host,port in [('MESH_WX','oldest',9001),('WX_ECHO','old',9002),('NOTICE_ECHO','new',9003)]:
        monkeypatch.setenv(prefix+'_HOST',host)
        monkeypatch.setenv(prefix+'_PORT',str(port))
        monkeypatch.setenv(prefix+'_DB',str(tmp_path/(host+'.db')))
    result=config.load_bootstrap()
    assert result.http_host=='new' and result.http_port==9003
    assert result.db_path==str(tmp_path/'new.db')


def test_wx_echo_environment_remains_supported(monkeypatch,tmp_path):
    monkeypatch.setenv('WX_ECHO_DB',str(tmp_path/'existing.db'))
    monkeypatch.setenv('WX_ECHO_HOST','legacy-host')
    monkeypatch.setenv('WX_ECHO_PORT','8110')
    result=config.load_bootstrap()
    assert result.db_path==str(tmp_path/'existing.db')
    assert result.http_host=='legacy-host' and result.http_port==8110


@pytest.mark.parametrize('platform,old_name,new_name',[
    ('linux','wx-echo','notice-echo'),('win32','WXEcho','NoticeEcho'),('darwin','WXEcho','NoticeEcho')])
def test_existing_data_directory_is_reused(monkeypatch,tmp_path,platform,old_name,new_name):
    monkeypatch.setattr(config.sys,'platform',platform)
    monkeypatch.setattr(config.Path,'home',lambda:tmp_path)
    monkeypatch.setenv('XDG_DATA_HOME',str(tmp_path))
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path))
    base=tmp_path/'Library'/'Application Support' if platform=='darwin' else tmp_path
    old=base/old_name
    old.mkdir(parents=True)
    (old/'wx-echo.db').write_text('existing data')
    assert config.default_data_dir()==old
    assert config.load_bootstrap().db_path==str(old/'wx-echo.db')
    assert not (base/new_name).exists()


def test_fresh_install_uses_new_directory_and_database(monkeypatch,tmp_path):
    monkeypatch.setattr(config.sys,'platform','linux')
    monkeypatch.setenv('XDG_DATA_HOME',str(tmp_path))
    result=config.load_bootstrap()
    assert result.db_path==str(tmp_path/'notice-echo'/'notice-echo.db')


@pytest.mark.parametrize('database',['wx-echo.db','mesh-wx.db'])
def test_existing_database_name_remains_in_use(monkeypatch,tmp_path,database):
    monkeypatch.setattr(config,'default_data_dir',lambda:tmp_path)
    (tmp_path/database).write_text('existing')
    assert config.load_bootstrap().db_path==str(tmp_path/database)
    assert not (tmp_path/'notice-echo.db').exists()


def test_launcher_preserves_legacy_host_and_port(monkeypatch):
    monkeypatch.setenv('WX_ECHO_HOST','127.0.0.2')
    monkeypatch.setenv('WX_ECHO_PORT','8110')
    assert launcher._env_with_legacy('WX_ECHO_HOST','127.0.0.1')=='127.0.0.2'
    assert launcher._env_with_legacy('WX_ECHO_PORT','8000')=='8110'
    assert os.environ['NOTICE_ECHO_PORT']=='8110'


def test_visible_branding_and_release_names_are_noticeecho():
    root=Path(__file__).parents[1]
    base=(root/'app/web/templates/base.html').read_text()
    assert '<title>Meshcore NoticeEcho' in base
    assert 'WXEcho' not in base and 'data:image' not in base
    assert 'name="NoticeEcho"' in (root/'packaging/noticeecho.spec').read_text()
    release=(root/'.github/workflows/release.yml').read_text()
    assert 'NoticeEcho-windows-' in release
    assert 'WXEcho-windows-' not in release
