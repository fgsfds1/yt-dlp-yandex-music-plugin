"""Shared pytest fixtures for the plugin test suite.

The tests double as plain scripts (`python tests/test_x.py`) — each file
has its own `load_plugin()` for that mode; under pytest the `mod` fixture
below provides the same module object (loaded once per session).
"""

import importlib.util
import os

import pytest

HERE = os.path.dirname(__file__)
PLUGIN_PATH = os.path.join(HERE, '..', 'yt_dlp_plugins', 'extractor',
                           'yandex_music_v2.py')


@pytest.fixture(scope='session')
def mod():
    spec = importlib.util.spec_from_file_location('ymv2', PLUGIN_PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m
