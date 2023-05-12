import aioconsole
import aiohttp
import asyncio

import json
import os
import re

from typing import Callable


class ResponseError(Exception):
    """Raise upon failing to request data from the Plasmo API"""

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

    API_LINK = "https://rp.plo.su/api"
    SERVERS = ("sur", "cr")

    # TODO: normalize docstring attribute types
    def __init__(self, players_path: str, settings_path: str, handler: Callable = None):
        self.players_path = players_path

        with open(self.players_path) as file:
            players = json.load(file)

        self.targeted_players = set(players)
        self.online_players = set()

        with open(settings_path) as file:
            settings = json.load(file)

        server, interval = settings["watchdog"].values()

        self.server = server if server in self.SERVERS else self.SERVERS[0]
        self.interval = max(15, interval)

        self.handler = handler if handler else lambda e: print(e)
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
        :param player_id: player's corresponding ID stored in Plasmo database
        """
        self.targeted_players.add(player_id)
        await self.save_changes()

    async def remove_player(self, player_id: int, player_nick: str = None) -> None:
        """
        Remove an entry from the list of targets
        :param player_id: player's corresponding ID stored in Plasmo database
        :param player_nick: player's nickname
        """
        self.targeted_players.remove(player_id)
        await self.save_changes()

        if player_nick and player_nick in self.online_players:
            self.online_players.remove(player_nick)

    async def request_plasmo(self, route: str) -> dict:
        """
        Request and handle (to certain extent) data from the Plasmo API
        :param route: URL path of the required method (e.g. /user)
        :return: dict: contents of the JSON object "data"
        """
        async with self.session.get(self.API_LINK + route) as response:
            content_type = response.headers.get("Content-Type")
            if not content_type == "application/json":
                raise ResponseError(
                    'API returned data with unsupported type of "%s"' % content_type,
                    "BAD_CONTENT_TYPE",
                    await response.text(),
                )

            status_code = response.status
            if not status_code == 200:
                raise ResponseError(
                    "API returned a status code of %s" % status_code,
                    "BAD_STATUS_CODE",
                    await response.json(),
                )

            data = await response.json()
            if not data["status"]:
                raise ResponseError(
                    "API returned an internal status of False",
                    "BAD_INTERNAL_STATUS",
                    data["error"],
                )
        return data["data"]

    async def get_online_players(self) -> set:
        """Get a list of all players currently connected to the configured server"""
        players, index = set(), 0
        while players_chunk := await self.request_plasmo(
            "/server/stats_players?tab=online&from=%s" % index
        ):
            players.update(
                [
                    player["last_name"]
                    for player in players_chunk
                    if player["on_server"] == self.server
                ]
            )
            if len(players_chunk) < 50:
                break
            index += 50
        return players

    async def assert_player(self, value: str | int) -> (bool, dict):
        """
        Check if a player should be monitored (has access and is not banned)
        :param value: a nickname or an ID of the player
        :return: tuple: (bool: suitability,
                        dict: player's ID and nickname)
        """
        known_param, unknown_param = (
            ("id", "nick") if isinstance(value, int) else ("nick", "id")
        )
        shortened_data = {known_param: value, unknown_param: None}

        try:
            data = await self.request_plasmo("/user/profile?%s=%s" % (known_param, value))
            shortened_data[unknown_param] = data[unknown_param]
        except ResponseError as error:
            if error.reference in ("BAD_STATUS_CODE", "BAD_INTERNAL_STATUS"):
                return False, shortened_data
            raise

        if "has_access" not in data or "banned" not in data:
            return False, shortened_data

        if not data["has_access"] or data["banned"]:
            return False, shortened_data

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

        if data["id"] not in self.targeted_players:
            await self.add_player(data["id"])
            print("Added %s to the list!" % field)
            # TODO: actual announcement
        else:
            await self.remove_player(data["id"], data["nick"])
            print("Removed %s from the list!" % field)
            # TODO: actual announcement

    async def execute(self) -> None:
        """
        Core functionality of the Announcer.
        Check if any targets joined or left the server
        and additionally remove unsuitable targets
        """
        requests = [self.assert_player(player) for player in self.targeted_players]
        results = await asyncio.gather(*requests)

        targeted_nicks = set()

        for assertion, data in results:
            if assertion:
                targeted_nicks.add(data["nick"])
                continue

            await self.remove_player(data["id"], data["nick"])
            print("Removing %s due to unmet conditions!" % data["id"])
            # TODO: actual announcement

        if not targeted_nicks:
            return

        active_players = await self.get_online_players()
        if not active_players:
            return

        for nick in targeted_nicks:
            if nick in active_players and nick not in self.online_players:
                self.online_players.add(nick)
                print("%s joined the game!" % nick)
                # TODO: actual announcement

        for nick in set(self.online_players):
            if nick not in active_players:
                self.online_players.remove(nick)
                print("%s left the game!" % nick)
                # TODO: actual announcement

    async def start_listener(self) -> None:
        """Start listening for and handling user inputs"""
        while True:
            try:
                field = await aioconsole.ainput()
                await self.handle_input(field.strip())
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
