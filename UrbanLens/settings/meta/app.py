"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    app.py                                                                                               *
*        Path:    /UrbanLens/settings/meta/app.py                                                                      *
*        Project: urbanlens                                                                                            *
*        Version: 1.0.0                                                                                                *
*        Created: 2024-02-19                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@urbanlens.org                                                                                 *
*        Copyright (c) 2024 Urban Lens                                                                                 *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2024-02-19     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""

from __future__ import annotations
from pathlib import Path
from enum import Enum

DEFAULT_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
DEFAULT_PATH_PARENTS = {
    'project_root': DEFAULT_ROOT,
    'base_dir': 'project_root',
    'backups_dir': 'project_root',
    'log_root': 'project_root',
    'media_root': 'base_dir',
    'downloads_dir': 'media_root',
    'exports_dir': 'media_root',
    'static_root': 'base_dir',
}