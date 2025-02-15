import logging as log
import contextlib
import json

from pathlib import Path
from platformdirs import PlatformDirs

from libflagship.megajank import pppp_decode_initstring
from libflagship.httpapi import AnkerHTTPAppApiV1, AnkerHTTPPassportApiV1
from libflagship.util import unhex

from .model import Serialize, Account, Printer, Config


class BaseConfigManager:

    def __init__(self, dirs: PlatformDirs, classes=None):
        self._dirs = dirs
        if classes:
            self._classes = {t.__name__: t for t in classes}
        else:
            self._classes = []
        dirs.user_config_path.mkdir(exist_ok=True, parents=True)

    @contextlib.contextmanager
    def _borrow(self, value, write, default=None):
        if not default:
            default = {}
        pr = self.load(value, default)
        yield pr
        if write:
            self.save(value, pr)

    @property
    def config_root(self):
        return self._dirs.user_config_path

    def config_path(self, name):
        return self.config_root / Path(f"{name}.json")

    def _load_json(self, val):
        if "__type__" not in val:
            return val

        typename = val["__type__"]
        if typename not in self._classes:
            return val

        return self._classes[typename].from_dict(val)

    @staticmethod
    def _save_json(val):
        if not isinstance(val, Serialize):
            return val

        data = val.to_dict()
        data["__type__"] = type(val).__name__
        return data

    def load(self, name, default):
        path = self.config_path(name)
        if not path.exists():
            return default

        return json.load(path.open(), object_hook=self._load_json)

    def save(self, name, value):
        path = self.config_path(name)
        path.write_text(json.dumps(value, default=self._save_json, indent=2) + "\n")


class AnkerConfigManager(BaseConfigManager):

    def modify(self):
        return self._borrow("default", write=True)

    def open(self):
        return self._borrow("default", write=False)


def configmgr(profile="default"):
    return AnkerConfigManager(PlatformDirs("ankerctl"), classes=(Config, Account, Printer))


def load_config_from_api(auth_token, region, insecure):
    log.info("Initializing API..")
    appapi = AnkerHTTPAppApiV1(auth_token=auth_token, region=region, verify=not insecure)
    ppapi = AnkerHTTPPassportApiV1(auth_token=auth_token, region=region, verify=not insecure)

    # request profile and printer list
    log.info("Requesting profile data..")
    profile = ppapi.profile()

    # create config object
    config = Config(account=Account(
        auth_token=auth_token,
        region=region,
        user_id=profile['user_id'],
        email=profile["email"],
    ), printers=[])

    log.info("Requesting printer list..")
    printers = appapi.query_fdm_list()

    log.info("Requesting pppp keys..")
    sns = [pr["station_sn"] for pr in printers]
    dsks = {dsk["station_sn"]: dsk for dsk in appapi.equipment_get_dsk_keys(station_sns=sns)["dsk_keys"]}

    # populate config object with printer list
    for pr in printers:
        station_sn = pr["station_sn"]
        config.printers.append(Printer(
            sn=station_sn,
            mqtt_key=unhex(pr["secret_key"]),
            wifi_mac=pr["wifi_mac"],
            ip_addr=pr["ip_addr"],
            api_hosts=pppp_decode_initstring(pr["app_conn"]),
            p2p_hosts=pppp_decode_initstring(pr["p2p_conn"]),
            p2p_duid=pr["p2p_did"],
            p2p_key=dsks[pr["station_sn"]]["dsk_key"],
        ))
        log.info(f"Adding printer [{station_sn}]")

    return config


def attempt_config_upgrade(config, profile, insecure):
    path = config.config_path("default")
    data = json.load(path.open())
    cfg = load_config_from_api(
        data["account"]["auth_token"],
        data["account"]["region"],
        insecure
    )

    # save config to json file named `ankerctl/default.json`
    config.save("default", cfg)
    log.info("Finished import")
