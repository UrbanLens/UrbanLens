"""*********************************************************************************************************************
*                                                                                                                      *

Django's command-line utility for administrative tasks.
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    manage.py                                                                                            *
*        Path:    /manage.py                                                                                           *
*        Project: UL                                                                                                   *
*        Version: <<projectversion>>                                                                                   *
*        Created: 2023-12-24                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@manlyphotos.com                                                                                 *
*        Copyright (c) 2023 Urban Lens                                                                                 *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2023-12-24     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""
#!/usr/bin/env python
import os
import sys

def main():
	"""Run administrative tasks."""
	#os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'UrbanLens.settings.local')
	# Temporarily Override the default settings module
	os.environ['DJANGO_SETTINGS_MODULE'] = 'UrbanLens.settings.local'
	try:
		from django.core.management import execute_from_command_line
	except ImportError as exc:
		raise ImportError(
			"Couldn't import Django. Are you sure it's installed and "
			"available on your PYTHONPATH environment variable? Did you "
			"forget to activate a virtual environment?"
		) from exc
	execute_from_command_line(sys.argv)


if __name__ == '__main__':
	main()