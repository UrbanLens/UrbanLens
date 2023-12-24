"""*****************************************************************************
 * 																			   *
 *	Initializes the project for the first time.								   *
 *																			   *
 *	This primarily asks for credentials (i.e. to databases, etc) and updates   *
 *	the settings files accordingly.											   *
 *                                                                             *
 * Metadata:                                                                   *
 *                                                                             *
 * 	File: init.py                                                              *
 * 	Project: Urban Lens                                                        *
 * 	Author: Jess Mann                                                          *
 * 	Email: jess@manlyphotos.com                                                *
 *                                                                             *
 * 	-----                                                                      *
 *                                                                             *
 *
 * 	Modified By: Jess Mann                                                     *
 *                                                                             *
 * 	-----                                                                      *
 *                                                                             *
 * 	Copyright (c) 2023 Urban Lens                                              *
 * 																			   *
 ****************************************************************************"""

from __future__ import annotations
import os
import sys
import re
import argparse
from typing import Optional
import yaml
from pathlib import Path
import subprocess
import logging
from init_bash import BashInit

logger = logging.getLogger(__name__)

# TODO: Buildstatic
# TODO: System check output

class UnrecoverableError(Exception):
	"""
	An error that cannot be recovered. This will result in a sys.exit(1)
	"""

class DjangoProjectInitializer:
	_db_host : str
	_db_port : int
	_db_name : str
	_db_user : str
	_db_pass : str
	_environment : str

	def __init__(self, no_runserver : bool = False, environment : Optional[str] = None):
		self.no_runserver = no_runserver

		# Get database details from environment variables
		self.db_host = os.environ.get('DB_HOST', 'localhost')
		self.db_port = os.environ.get('DB_PORT', 5432)
		self.db_name = os.environ.get('DB_NAME', 'UrbanLens')
		self.db_user = os.environ.get('DB_USER', 'postgres')
		self.db_pass = os.environ.get('DB_PASS', 'postgres')
		self.environment = environment if environment else os.environ.get('ENVIRONMENT', 'production')

	@property
	def db_host(self) -> str:
		return self._db_host

	@db_host.setter
	def db_host(self, value : str):
		# Strip special characters from the host
		self._db_host = re.sub(r'[^a-zA-Z0-9_-]', '', value)
		if value != self._db_host:
			# Only log the "safe" value to prevent injection attacks into the logfile
			logger.error('Invalid host name. Stripped special characters to %s', self._db_host)
			raise UnrecoverableError("Invalid host name.")

	@property
	def db_port(self) -> int:
		return self._db_port

	@db_port.setter
	def db_port(self, value : int):
		# validate that value is a number
		try:
			self._db_port = int(value)
		except ValueError:
			logger.error('Invalid port number')
			raise UnrecoverableError()

	@property
	def db_name(self) -> str:
		return self._db_name

	@db_name.setter
	def db_name(self, value : str):
		# Strip special characters from the name
		self._db_name = re.sub(r'[^a-zA-Z0-9_-]', '', value)
		if value != self._db_name:
			# Only log the "safe" value to prevent injection attacks into the logfile
			logger.error('Invalid database name. Stripped special characters to %s', self._db_name)
			raise UnrecoverableError("Invalid database name.")

	@property
	def db_user(self) -> str:
		return self._db_user

	@db_user.setter
	def db_user(self, value : str):
		# Strip special characters from the user
		self._db_user = re.sub(r'[^a-zA-Z0-9_-]', '', value)
		if value != self._db_user:
			# Only log the "safe" value to prevent injection attacks into the logfile
			logger.error('Invalid database user. Stripped special characters to %s', self._db_user)
			raise UnrecoverableError("Invalid database user.")

	@property
	def db_pass(self) -> str:
		return self._db_pass

	@db_pass.setter
	def db_pass(self, value : str):
		# Strip special characters from the pass
		self._db_pass = re.sub(r'[^a-zA-Z0-9!@#$^*()_-]', '', value)
		if value != self._db_pass:
			# Only log the "safe" value to prevent injection attacks into the logfile
			logger.error('Invalid database password. Stripped special characters.')
			raise UnrecoverableError("Invalid database password.")

	@property
	def environment(self) -> str:
		return self._environment

	@environment.setter
	def environment(self, value : str):
		# Ensure environment is a known option
		if value not in ['development', 'test', 'production']:
			safe_value = re.sub(r'[^a-zA-Z0-9_-]', '', value)
			logger.error(f'Invalid environment: {safe_value}')
			raise UnrecoverableError(f'Invalid environment: {safe_value}')

		self._environment = value

	def clone_repo(self, destination_path : str = '/rita'):
		"""
		Clone the github repo at UrbanLens/UrbanLens
		"""
		# get the git username and email from environment variables
		name = os.environ.get('GIT_NAME')
		email = os.environ.get('GIT_EMAIL')
		token = os.environ.get('GH_TOKEN')
		dir_exists : bool = False

		if not name or not email or not token:
			logger.error('GIT_NAME, GIT_EMAIL, or GH_TOKEN environment variables are not set. Cannot initialize git project.')
			raise UnrecoverableError()

		# Ensure destination_path doesnt already exist
		if Path(destination_path).exists():
			dir_exists = True

			# If the path exists, check if it is entirely empty
			if len(list(Path(destination_path).iterdir())) > 0:
				logger.error(f'Destination path {destination_path} already exists. Cannot clone project.')
				raise UnrecoverableError()

			# If the path exists but is empty, delete it
			os.rmdir(destination_path)
			logger.info(f'Deleted empty directory {destination_path}.')

		try:
			# Set username and email based on ENV variables
			try:
				subprocess.run(['git', 'config', '--global', 'user.name', name], check=True)
				subprocess.run(['git', 'config', '--global', 'user.email', email], check=True)
			except subprocess.CalledProcessError as e:
				logger.error(f'Error occurred during git config: {e}')
				raise UnrecoverableError() from e

			# Run gh repo clone UrbanLens/UrbanLens
			try:
				subprocess.run(['gh', 'repo', 'clone', 'UrbanLens/UrbanLens', destination_path], check=True)
				logger.info('Cloned UrbanLens/UrbanLens as %s <%s> to %s.', name, email, destination_path)
			except subprocess.CalledProcessError as e:
				logger.error('Error occurred during gh repo clone as %s <%s> into %s: %s', name, email, destination_path, e)
				raise UnrecoverableError() from e

			# Ensure log directories exist, and create empty logfiles
			try:
				log_dir = Path(os.path.join('/var', 'log', 'rita'))
				if not log_dir.exists():
					os.mkdir(log_dir)
					logger.debug(f'Created directory {log_dir}')
				for log_file in ['app.log', 'debugging.log', 'psql.log', 'test.log']:
					log_path = os.path.join(log_dir, log_file)
					if not Path(log_path).exists():
						with open(log_path, 'w') as file:
							file.write('')
						logger.debug(f'Created empty logfile {log_path}')
			except IOError as e:
				# Trigger an error (which may print to the screen, or show up in a logfile that IS available)
				logger.error('Error creating logfiles. Will continue, but errors may occur and be unlogged: %s', e)

		finally:
			# In the case of an error, ensure our environment is reset back to what we expect.
			# If the directory already existed before we deleted it, but doesnt now, recreate it
			if dir_exists and not Path(destination_path).exists():
				os.mkdir(destination_path, exists_ok=True)
				logger.info(f'Recreated directory {destination_path} after error.')

	def init_db(self):
		"""
		Create the "UrbanLens" database in postgres
		"""
		if self.check_db():
			logger.info('Database UrbanLens already exists.')
			return

		logger.info('Database UrbanLens does not exist. Creating...')

		# Create the database
		try:
			subprocess.run(['psql', '-U', 'postgres', '-h', 'rita_db', '-W', 'postgres', '-c', 'CREATE DATABASE "UrbanLens"'], check=True)
			logger.info('Created database UrbanLens.')
		except subprocess.CalledProcessError as e:
			logger.error('Error creating database UrbanLens: %s', e)
			raise UnrecoverableError() from e

		if not self.check_db():
			logger.error('Database UrbanLens was not created.')
			raise UnrecoverableError('Database UrbanLens was not created.')

	def copy_sample_credentials(self):
		"""
		Copies Credentials-sample.yaml to Credentials.yaml

		Raises:
			UnrecoverableError: if the file cannot be copied
		"""
		try:
			with open('/rita/UrbanLens/Credentials-sample.yaml', 'r') as sample_file:
				sample_data = sample_file.read()
			with open('/rita/UrbanLens/Credentials.yaml', 'w') as new_file:
				new_file.write(sample_data)
			logger.info('Copied Credentials-sample.yaml to Credentials.yaml.')
		except IOError as e:
			logger.error(f'Error copying Credentials-sample.yaml: {e}')
			raise UnrecoverableError() from e

		# Check that it now exists
		if not Path('/rita/UrbanLens/Credentials.yaml').exists():
			logger.error('Credentials-sample.yaml was copied but still does not exist.')
			raise UnrecoverableError('Credentials-sample.yaml was copied but Credentials file still does not exist.')

	def update_credentials(self):
		"""
		Updates the Peoplesoft DB credentials in Credentials.yaml

		Raises:
			UnrecoverableError: if the file cannot be updated
		"""
		try:
			with open('/rita/UrbanLens/Credentials.yaml', 'r') as file:
				data = yaml.safe_load(file)

			# Attempt to get them from the environment vars first
			username = os.environ.get('PS_USERNAME')
			password = os.environ.get('PS_PASSWORD')
			if username:
				data['DB']['peoplesoft']['Username'] = username
			if password:
				data['DB']['peoplesoft']['Password'] = password

			'''
			if not username:
				username = input('Enter your Peoplesoft DB username: ').strip()
			if not password:
				password = getpass('Enter your Peoplesoft DB password: ').strip()
			'''
			if not data['DB']['peoplesoft']['Username'] or not data['DB']['peoplesoft']['Password']:
				logger.warning('Data from Peoplesoft will be disabled, due to missing PS_USERNAME or PS_PASSWORD environment variables.')


			# Set datacache properties
			props = {
				'db_host': 'Host',
				'db_port': 'Port',
				'db_name': 'Database',
				'db_user': 'Username',
				'db_pass': 'Password',
			}

			for prop_key, data_key in props.items():
				prop = getattr(self, prop_key)
				if prop:
					data['DB']['datacache'][data_key] = prop

			with open('/rita/UrbanLens/Credentials.yaml', 'w') as file:
				yaml.dump(data, file)
			logger.info('Updated Peoplesoft DB credentials in Credentials.yaml.')
		except IOError as e:
			logger.error(f'Error updating Credentials.yaml: {e}')
			raise UnrecoverableError() from e

		# Check that it now exists
		if not Path('/rita/UrbanLens/Credentials.yaml').exists():
			logger.error('Credentials.yaml was updated but still does not exist.')
			raise UnrecoverableError('Credentials.yaml was updated but still does not exist.')

	def copy_sample_env(self):
		"""
		Copies .env-sample to .env

		Raises:
			UnrecoverableError: if the file cannot be copied
		"""
		try:
			with open('/rita/UrbanLens/.env-sample', 'r') as sample_file:
				sample_data = sample_file.read()
			with open('/rita/UrbanLens/.env', 'w') as new_file:
				new_file.write(sample_data)
			logger.info('Copied .env-sample to .env.')
		except IOError as e:
			logger.error(f'Error copying .env-sample: {e}')
			raise UnrecoverableError() from e

		# Check that it now exists
		if not Path('/rita/UrbanLens/.env').exists():
			logger.error('.env was copied but still does not exist.')
			raise UnrecoverableError('.env was copied but still does not exist.')

	def update_env(self, username : str, email : str):
		"""
		Updates the env file with the git username and email

		Probably deprecated

		Args:
			username (str): git username
			email (str): git email

		Raises:
			UnrecoverableError: if the file cannot be updated
		"""
		try:
			with open('/rita/UrbanLens/.env', 'r') as file:
				data = file.readlines()

			for i, line in enumerate(data):
				if line.startswith('GIT_USERNAME='):
					data[i] = f'GIT_USERNAME={username}\n'
				elif line.startswith('GIT_EMAIL='):
					data[i] = f'GIT_EMAIL={email}\n'

			with open('/rita/UrbanLens/.env', 'w') as file:
				file.writelines(data)
			logger.info('Updated git username and email in .env.')
		except IOError as e:
			logger.error(f'Error updating .env: {e}')
			raise UnrecoverableError() from e

		# Check that it now exists
		if not Path('/rita/UrbanLens/.env').exists():
			logger.error('.env was updated but still does not exist.')
			raise UnrecoverableError('.env was updated but still does not exist.')

	def npm_init(self):
		"""
		Runs npm init

		Raises:
			UnrecoverableError: if npm init fails
		"""
		try:
			# Initialize npm
			#subprocess.run(['npm', 'init', '-gy'], check=True, cwd='/rita/UrbanLens')
			subprocess.run(['npm', 'install', '-y'], check=True, cwd='/rita/UrbanLens')
			logger.info('npm packages installed.')
		except subprocess.CalledProcessError as e:
			logger.error(f'Error occurred during npm init: {e}')
			raise UnrecoverableError() from e

	def build_frontend(self):
		"""
		Builds the frontend

		Raises:
			UnrecoverableError: if the frontend fails to build
		"""
		# First, ensure that all directories for build files exist.
		# This is necessary because the build process will not create them, and will fail if they do not exist.
		apps = [ 'dashboard', 'peoplesoft', 'core' ]
		dirs = []
		for app in apps:
			dirs.append(os.path.join('/rita', 'UrbanLens', app, 'frontend', 'static', app, 'js'))
			dirs.append(os.path.join('/rita', 'UrbanLens', app, 'frontend', 'static', app, 'css'))

		for dir in dirs:
			if not Path(dir).exists():
				os.makedirs(dir)
				logger.debug(f'Created directory {dir}')

		# Ensure entrypoint (dashboard/frontend/static/dashboard/js/index.js) exists
		entry = os.path.join('/rita', 'UrbanLens', 'dashboard', 'frontend', 'static', 'dashboard', 'js', 'index.js')
		if not Path(entry).exists():
			with open(entry, 'w') as file:
				file.write('')
			logger.debug(f'Created empty file {entry}')

		try:
			match self.environment:
				case 'development':
					command = ['npm', 'run', 'build']
				case _:
					command = ['npm', 'run', 'deploy']

			# Run the build
			subprocess.run(command, check=True, cwd='/rita/UrbanLens')
			'''
			# Run in a separate process and continue without waiting for it to end
			process = subprocess.Popen(command, cwd='/rita/UrbanLens')

			# Grab the output and when we encounter success message, we know the build is complete
			logger.info('Waiting for frontend to be built...')
			for _i in range(999999999):
				if process.stdout:
					line = process.stdout.readline()
					content = line.decode('utf-8').strip()
					logger.debug(content)

					if line and re.search(r'webpack [\d.]+ compiled successfully in \d+ ms', content):
						logger.info('Webpack complete')
						break

				time.sleep(0.1)
			'''

			logger.debug('Collecting static files...')
			# Collect static files for django
			subprocess.run(['python', 'manage.py', 'collectstatic', '--noinput'], check=True, cwd='/rita/UrbanLens')
			logger.info('Frontend built.')
		except subprocess.CalledProcessError as e:
			logger.error(f'Error occurred during frontend build: {e}')
			raise UnrecoverableError() from e

	def run_migrations(self):
		"""
		Runs django migrations (i.e. creates the django DB tables)

		Raises:
			UnrecoverableError: if the migrations fails
		"""
		try:
			subprocess.run(['python', 'manage.py', 'migrate'], check=True, cwd='/rita/UrbanLens')
			logger.info('Migrations completed.')
		except subprocess.CalledProcessError as e:
			logger.error(f'Error occurred during migration: {e}')
			raise UnrecoverableError() from e

	def run_dev_server(self):
		"""
		Runs the development server

		Raises:
			UnrecoverableError: if the server fails to run
		"""
		try:
			subprocess.run(['python', 'manage.py', 'runserver'], check=True, cwd='/rita/UrbanLens')
		except subprocess.CalledProcessError as e:
			logger.error(f'Error occurred while running the server: {e}')
			raise UnrecoverableError() from e

	def run_prod_server(self):
		"""
		Runs the production server

		Raises:
			UnrecoverableError: if the server fails to run
		"""
		try:
			# TODO: run this through npm or app.py, so changes to the method for running are consistent
			subprocess.run(["gunicorn", "UrbanLens.wsgi:application", "--bind", "0.0.0.0:8000"], check=True, cwd='/rita/UrbanLens')
		except subprocess.CalledProcessError as e:
			logger.error(f'Error occurred while running the server: {e}')
			raise UnrecoverableError() from e

	def check_network(self) -> bool:
		"""
		Checks that we have a functioning network connection

		Returns:
			bool: True if the network is functioning, False otherwise
		"""
		# Check that we can ping google.com
		try:
			subprocess.run(['ping', '-c', '1', 'google.com'], check=True)
		except subprocess.CalledProcessError as e:
			logger.error(f'No Network Connection in container: {e}')
			return False

		return True

	def fix_network(self) -> bool:
		"""
		In the case of a network problem, run the init_bash.py script to attempt a fix

		Returns:
			bool: True if the network was fixed, False otherwise

		Raises:
			UnrecoverableError: if the network cannot be fixed
		"""

		# Get the path for the init_bash.py script (same directory as this script)
		script_path = Path(__file__).parent / 'init_bash.py'

		# Run init_bash.py at the end of ~/.bashrc
		try:
			with open('/root/.bashrc', 'a') as file:
				file.write(f'python {script_path}\n')
			logger.info('Upaded /root/.bashrc to run init_bash.py.')
		except IOError as e:
			logger.error(f'Error updating ~/.bashrc: {e}')

		bash = BashInit()
		result = bash.run()

		if not result:
			logger.error('DNS nameserver is not correctly set. Network issues may occur.')
			raise UnrecoverableError('DNS nameserver could not be fixed. Network issues may occur.')

		return True

	def check_ssh_keys(self) -> bool:
		"""
		Check that SSH keys exist and are valid for checking out source code.

		Returns:
			bool: True if the keys exist and are valid, False otherwise
		"""
		if not os.path.exists('/root/.ssh/id_rsa') or not os.path.exists('/root/.ssh/id_rsa.pub'):
			logger.error('SSH keys do not exist.')
			return False

		# Check that the keys are valid for gitlab
		server = 'gitlab.com'
		try:
			subprocess.run(['ssh', '-T', server, '-i', '/root/.ssh/id_rsa'], check=True)
		except subprocess.CalledProcessError:
			logger.error('SSH keys are not valid for %s.', server)
			return False

		return True

	def check_dependencies(self):
		"""
		TODO
		"""
		raise NotImplementedError()

	def install_dependencies(self):
		"""
		TODO
		"""
		raise NotImplementedError()

	def check_db(self) -> bool:
		"""
		Checks that the database exists.

		Returns:
			bool: True if the database exists, False otherwise
		"""
		try:
			subprocess.run(['psql', '-U', 'postgres', '-h', 'rita_db', '-W', 'postgres', '-c', f'SELECT 1 FROM pg_database WHERE datname=\'{self.db_name}\''], check=True)
		except subprocess.CalledProcessError:
			logger.debug('Database %s does not exist.', self.db_name)
			return False

		return True

	def initialize_project(self):
		"""
		Initializes the project for the first time

		Raises:
			UnrecoverableError: if the project cannot be initialized
		"""
		# Clone the repo
		if Path('/rita').exists():
			logger.warning('Project already exists. Skipping init.')
			return

		# Fix common network problems so we don't encounter a network error fetching remote resources
		if not self.check_network():
			logger.warning('Network was not functioning. Trying a change to the nameserver. Some Docker networking may not work after this.')
			self.fix_network()
			if not self.check_network():
				logger.error('Network is not functioning, even after DNS fix. Cannot initialize project.')
				raise UnrecoverableError("Network is not functioning, even after DNS fix. Cannot initialize project.")

		'''
		if not self.check_ssh_keys():
			logger.error('SSH keys are not valid. Cannot initialize project.')
			raise UnrecoverableError("SSH keys are not valid. Cannot initialize project.")
		'''
		self.clone_repo()

		if not Path('/rita/UrbanLens/Credentials.yaml').exists():
			self.copy_sample_credentials()
			self.update_credentials()

		if not Path('/rita/UrbanLens/.env').exists():
			self.copy_sample_env()

		# Install and build the frontend
		self.npm_init()
		self.build_frontend()

		# Setup the DB
		self.init_db()
		self.run_migrations()

		if not self.no_runserver:
			self.run_prod_server()

def main():
	"""
	Run the initializer.
	"""
	logging.basicConfig(
			level=logging.INFO,
			format='%(asctime)s - %(levelname)s - %(message)s',
			handlers=[
				logging.StreamHandler(),
				logging.FileHandler(os.path.join('/var', 'log', 'rita', 'init.log')),
			]
		)

	parser = argparse.ArgumentParser(description='Initialize Django project and run server')
	parser.add_argument('--no-runserver', '-x', action='store_true', help='Do not run the development server after migration')
	parser.add_argument('--debug', '-v', action='store_true', help='Enable debug logging')
	parser.add_argument('--environment', '-e', choices=['development', 'test', 'production'], help='Set the environment')
	args = parser.parse_args()

	if args.debug:
		# Replace root logger after config change (I'm not certain this is necessary TODO)
		#logger = logging.getLogger(__name__)
		# Change the loglevel
		logger.setLevel(logging.DEBUG)
		logger.info('Debug logging enabled.')

	try:
		initializer = DjangoProjectInitializer(no_runserver=args.no_runserver, environment=args.environment)
		initializer.initialize_project()
	except KeyboardInterrupt:
		logger.info('Initialization cancelled.')
		sys.exit(0)
	except UnrecoverableError:
		logger.error('Initialization failed.')
		sys.exit(1)

	sys.exit(0)

if __name__ == '__main__':
	"""
	If the script is called directly...
	"""
	main()