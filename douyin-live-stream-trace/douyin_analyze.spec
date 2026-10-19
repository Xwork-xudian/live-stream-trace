# -*- mode: python ; coding: utf-8 -*-
block_cipher = None

a = Analysis(
    ['analyze_room.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('mappings', 'mappings'),
    ],
    hiddenimports=[
        'requests',
        'urllib3',
        'charset_normalizer',
        'certifi',
        'idna',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'playwright',
        'tshark',
        'pyshark',
        'tkinter',
        'matplotlib',
        'numpy',
        'pandas',
        'PIL',
        'scipy',
    ],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure, cipher=block_cipher)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='douyin-live-stream-trace',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    icon=None,
)
