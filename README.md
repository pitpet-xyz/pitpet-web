# *PitPet*

Codebase forked from https://github.com/epilys/sic

<table align="center">
	<tbody>
		<tr>
			<td><kbd><img src="./screenshot-frontpage.png" alt="frontpage screenshot" title="frontpage screenshot" height="250"/></kbd></td>
			<td><kbd><img src="./screenshot-frontpage-mobile.png" alt="frontpage on mobile screenshot"  height="250"/></kbd></td>
		</tr>
	</tbody>
</table>

## Setup / Deployment

```shell
cp sic/local/secret_settings.py{.template,}
vim sic/local/secret_settings.py # REQUIRED: add secret token
vim sic/local/settings_local.py # OPTIONAL: local settings (SMTP etc)
python3 -m venv # OPTIONAL: setup virtual python enviroment in 'venv' directory
python3 -m pip install -r requirements.txt # Or 'pip3' install...
python3 manage.py migrate #sets up database
python3 manage.py createsuperuser #selfexplanatory
python3 manage.py runserver # run at 127.0.0.1:8000
python3 manage.py runserver 8001 # or run at 127.0.0.1:8001
python3 manage.py runserver 0.0.0.0:8000 # or run at public-ip:8000
```

See [`DEPLOY.md`](DEPLOY.md) for deployment instructions.

## Initialize database with example objects

Seed data files are located under `sic/fixtures`.

```shell
$ ls sic/fixtures
tags.json
```

You can load them in the database with:

```shell
$ python3 manage.py loaddata tags
Installed 8 object(s) from 1 fixture(s)
```

## Code style

See [`CODE_STYLE.md`](CODE_STYLE.md).
