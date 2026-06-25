
from urbanlens.UrbanLens.environments.meta import EnvironmentTypes, DebugTypes
from urbanlens.UrbanLens.environments.base import BaseEnvironment

from urbanlens.UrbanLens.environments.local import Local
from urbanlens.UrbanLens.environments.dev import Development
from urbanlens.UrbanLens.environments.test import Testing
from urbanlens.UrbanLens.environments.staging import Staging
from urbanlens.UrbanLens.environments.prod import Production
from urbanlens.UrbanLens.environments.factory import select_environment