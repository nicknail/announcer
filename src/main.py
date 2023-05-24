import aiohttp
import asyncio

import json
import os
import re

from typing import Callable


class ResponseError(Exception):
    """Raise upon failing to request data from the API"""

    def __init__(self, message, reference, body):
        super().__init__(message)

        self.reference = reference
        self.body = body


class Announcer:
    """
    Asynchronous watchdog for monitoring player activity on Plasmo RP,
    making use of the Plasmo and Telegram Bot APIs

    :param players_path: path to the file with the list of targeted players
    :param settings_path: path to the Announcer configuration file
    :param handler: outer function for handling unexpected API behaviour
    """

    PLASMO_API = "https://rp.plo.su/api"
    PLASMO_SERVERS = ("sur", "cr")

    # TODO: fix docstring attribute types
    def __init__(
        self,
        players_path: str,
        settings_path: str,
        handler: Callable = lambda e: print(e),
    ):
        self.players_path = players_path
        with open(players_path) as file:
            players = json.load(file)

        self.targeted_players = set(players)
        self.online_players = set()

        with open(settings_path) as file:
            settings = json.load(file)

        # TODO: ???
        self.servers = (
            settings["watchdog"]["servers"]
            if set(settings["watchdog"]["servers"]).issubset(set(self.PLASMO_SERVERS))
            else self.PLASMO_SERVERS[0]
        )
        self.interval = max(15, settings["watchdog"]["interval"])

        self.TELEGRAM_API = "https://api.telegram.org/bot%s" % settings["bot"]["token"]
        self.owners = settings["bot"]["owners"]

        self.handler = handler

        self.offset = 0
        self.session = aiohttp.ClientSession()

    async def save_changes(self) -> None:
        """
        Export the list of player IDs to the configured file,
        wiping all the previously saved entries in the process
        """
        with open(self.players_path, "w") as file:
            json.dump(list(self.targeted_players), file)

    async def add_player(self, player_id: int) -> None:
        """
        Add an entry to the list of targets
        :param player_id: player's corresponding ID
        """
        self.targeted_players.add(player_id)
        await self.save_changes()

    async def remove_player(self, player_id: int, player_nick: str = None) -> None:
        """
        Remove an entry from the list of targets
        :param player_id: player's corresponding ID
        :param player_nick: player's nickname
        """
        self.targeted_players.remove(player_id)
        await self.save_changes()

        if player_nick and player_nick in self.online_players:
            self.online_players.remove(player_nick)

    async def query_plasmo(self, route: str, payload: dict) -> dict:
        """
        Request and handle (to certain extent) data from the Plasmo API
        :param route: URL path of the method (e.g. /user)
        :param payload: URL query string
        :return: dict: contents of the JSON object "data"
        """
        print(route, payload)
        async with self.session.get(
            self.PLASMO_API + route, params=payload
        ) as response:
            content_type = response.headers.get("Content-Type")
            if not content_type == "application/json":
                raise ResponseError(
                    'API returned data with unsupported type of "%s"' % content_type,
                    "BAD_CONTENT_TYPE",
                    await response.text(),
                )

            data = await response.json()
            if not data["status"]:
                raise ResponseError(
                    "API returned an internal status of False",
                    "BAD_INTERNAL_STATUS",
                    data["error"]["msg"],
                )
        return data["data"]

    async def query_telegram(self, route: str, payload: dict) -> dict:
        """
        Request and handle data from the Telegram Bot API
        :param route: URL path of the required method (e.g. /getMe)
        :param payload: URL query string
        :return: dict: contents of the JSON object "result"
        """
        print(route, payload)
        async with self.session.get(
            self.TELEGRAM_API + route, params=payload
        ) as response:
            data = await response.json()
            if not data["ok"]:
                raise ResponseError(
                    "API returned an internal status of False",
                    "BAD_INTERNAL_STATUS",
                    data["description"],
                )
        return data["result"]

    @staticmethod
    async def format_link(nick: str) -> str:
        # TODO: docstring here
        return "[{0}](https://rp.plo.su/u/{0})".format(nick) if nick else "N/A"

    async def send_message(self, fields: list):
        # TODO: docstring & weird attribute name
        requests = (
            self.query_telegram(
                "/sendMessage",
                {
                    "chat_id": owner,
                    "text": "\n".join(fields),
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": "true",
                },
            )
            for owner in self.owners
        )
        await asyncio.gather(*requests)

    async def assert_player(self, value: str | int) -> (bool, dict):
        """
        Check if a player should be monitored (has access and is not banned)
        :param value: a nickname or an ID of the player
        # TODO: ???
        :return: tuple: (suitability; player's ID, nickname and status)
        """
        known_param, unknown_param = (
            ("id", "nick") if isinstance(value, int) else ("nick", "id")
        )
        shortened_data = {known_param: value, unknown_param: None, "server": None}

        try:
            data = await self.query_plasmo(
                "/user/profile", {known_param: value, "fields": "stats"}
            )
            shortened_data[unknown_param] = data[unknown_param]
        except ResponseError as error:
            if not error.reference == "BAD_CONTENT_TYPE":
                return False, shortened_data
            raise

        if "has_access" not in data or "banned" not in data:
            return False, shortened_data

        if not data["has_access"] or data["banned"]:
            return False, shortened_data

        shortened_data["server"] = data["stats"]["on_server"]
        return True, shortened_data

    async def handle_input(self, field: str) -> None:
        """
        Check if an inputted string is an actual nickname and add/remove
        its owner from to the list of targets if necessary
        :param field: an inputted string
        """
        if not re.fullmatch(r"[a-zA-Z0-9_]{3,16}", field):
            return

        assertion, data = await self.assert_player(field)
        if not assertion:
            return

        player_id, nick = data["id"], data["nick"]
        link = await self.format_link(nick)

        if player_id not in self.targeted_players:
            await self.add_player(player_id)
            await self.send_message(["➕ %s added" % link])
            print("Added %s to the list!" % field)
        else:
            await self.remove_player(player_id, nick)
            await self.send_message(["➖ %s removed" % link])
            print("Removed %s from the list!" % field)

    async def execute(self) -> None:
        """
        Core functionality of the Announcer.
        Check if any targets joined or left the server
        and additionally remove unsuitable targets
        """
        requests = (self.assert_player(player) for player in self.targeted_players)
        results = await asyncio.gather(*requests)

        messages = []

        for assertion, data in results:
            player_id, nick, server = data["id"], data["nick"], data["server"]
            link = await self.format_link(nick)

            if not assertion:
                await self.remove_player(player_id, nick)
                messages.append("➖ %s removed" % link)
                print("Removing %s (%s) due to unmet conditions!" % (nick, player_id))
                continue

            if server in self.servers and nick not in self.online_players:
                self.online_players.add(nick)
                messages.append("🟢 %s joined %s" % (link, server.upper()))
                print("%s joined the game!" % nick)

            if server not in self.servers and nick in self.online_players:
                self.online_players.remove(nick)
                messages.append("🔴 %s left" % link)
                print("%s left the game!" % nick)

        if messages:
            await self.send_message(messages)

    async def get_updates(self) -> None:
        payload = {"offset": self.offset, "timeout": 60, "allowed_updates": "message"}
        result = await self.query_telegram("/getUpdates", payload)
        for data in result:
            self.offset = max(self.offset, data["update_id"] + 1)

            if "message" not in data:
                continue

            if data["message"]["from"]["id"] not in self.owners:
                continue

            if "text" not in data["message"]:
                continue
            await self.handle_input(data["message"]["text"])

    async def start_listener(self) -> None:
        """Start listening for and handling user inputs"""
        while True:
            try:
                await self.get_updates()
            except ResponseError as error:
                self.handler(error)

    async def start_looper(self) -> None:
        """Start the loop of repeating lookups"""
        while True:
            try:
                await self.execute()
            except ResponseError as error:
                self.handler(error)
            await asyncio.sleep(self.interval)


async def main() -> None:
    script_path = os.path.dirname(os.path.realpath(__file__))
    players_path = script_path + "/../config/players.json"
    settings_path = script_path + "/../config/settings.json"

    announcer = Announcer(players_path, settings_path)

    li = asyncio.ensure_future(announcer.start_listener())
    lo = asyncio.ensure_future(announcer.start_looper())

    await li
    await lo


if __name__ == "__main__":
    try:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        pass
