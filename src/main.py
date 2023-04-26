import asyncio
import re
from copy import copy

import aioconsole
import aiohttp


class ResponseError(Exception):
    def __init__(self, message, reference, response):
        super().__init__(message)

        self.reference = reference
        self.response = response


class Announcer:
    """
    Asynchronous watchdog for monitoring player activity on Plasmo RP,
    making use of the Plasmo API and notifications daemon in Linux

    :param filename: Path to the file containing list of targeted players
    :param server: Targeted server type: "sur" (prp.plo.su) or "cr" (crp.plo.su)
    :param interval: Frequency of the lookups, in seconds
    """

    V1_LINK = "https://rp.plo.su/api"
    SERVERS = ("sur", "cr")

    def __init__(self, filename: str, server: str = "sur", interval: int = 60):
        self.filename = filename
        self.server = server if server in self.SERVERS else self.SERVERS[0]
        self.interval = max(15, interval)

        self.targeted_players = self.import_players()
        self.online_players = set()

    def import_players(self) -> set:
        """Import the list of player IDs whose activity needs to be monitored"""
        with open(self.filename) as file:
            file_content = file.read()

        if file_content:
            return set([int(s) for s in file_content.split(",")])
        else:
            return set()

    async def export_players(self) -> None:
        """
        Export the list of player IDs to the specified file,
        wiping all the previously saved entries in the process
        """
        formatted_list = ",".join([str(i) for i in self.targeted_players])
        with open(self.filename, "w") as file:
            file.write(formatted_list)

    async def add_player(self, player_id: int) -> None:
        """
        Add an entry to the list of targeted players
        :param player_id: player's corresponding ID stored in Plasmo database
        """
        self.targeted_players.add(player_id)
        await self.export_players()

    async def remove_player(self, player_id: int, player_nick: str = None) -> None:
        """
        Remove an entry from the list of targeted players
        :param player_id: player's corresponding ID stored in Plasmo database
        :param player_nick: player's nickname
        """
        self.targeted_players.remove(player_id)
        await self.export_players()

        if player_nick and player_nick in self.online_players:
            self.online_players.remove(player_nick)

    async def send_request(self, route: str, session: aiohttp.ClientSession) -> dict:
        """
        Request and handle (to certain extent) data from the Plasmo API
        :param route: URL path of the required method (e.g. /user)
        :param session: interface provided by aiohttp module
        :return: dict: contents of the JSON object "data"
        """
        print(route)
        async with session.get(self.V1_LINK + route) as response:
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
                    await response.json(),
                )
        return data["data"]

    async def get_online_players(self) -> set:
        """Get a list of all players currently connected to the specified server"""
        players, index = set(), 0
        async with aiohttp.ClientSession() as session:
            while players_chunk := await self.send_request(
                "/server/stats_players?tab=online&from=%s" % index, session
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

    async def assert_player(
        self, value: str | int, session: aiohttp.ClientSession | None = None
    ) -> (bool, dict):
        """
        Check if a player can be monitored (has access and is not banned)
        :param value: a nickname or an ID of the player
        :param session: interface provided by aiohttp module
        :return: tuple: (bool: assertion,
                        dict: player's ID and nickname)
        """
        param = "nick" if isinstance(value, str) else "id"

        data = None
        try:
            if session:
                data = await self.send_request(
                    "/user/profile?%s=%s" % (param, value),
                    session,
                )
            else:
                async with aiohttp.ClientSession() as acc_session:
                    data = await self.send_request(
                        "/user/profile?%s=%s" % (param, value),
                        acc_session,
                    )
        except ResponseError as error:
            if error.reference == "BAD_INTERNAL_STATUS":
                return False, {param: value}

        shortened_data = {"nick": data["nick"], "id": data["id"]}

        if "has_access" not in data or "banned" not in data:
            return False, shortened_data

        if not data["has_access"] or data["banned"]:
            return False, shortened_data

        return True, shortened_data

    async def handle_input(self, field: str) -> None:
        """
        Check if an inputted string is an actual nickname and add/remove
        its owner from to the list of targeted players if necessary
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
            # TODO
        else:
            await self.remove_player(data["id"], data["nick"])
            print("Removed %s from the list!" % field)
            # TODO

    async def execute(self) -> None:
        """
        Core functionality of the Announcer.
        Check if any players joined or left the specified server
        """
        async with aiohttp.ClientSession() as session:
            players = [self.assert_player(p, session) for p in self.targeted_players]
            results = await asyncio.gather(*players)

        targeted_nicks = set()

        for assertion, data in results:
            if assertion:
                targeted_nicks.add(data["nick"])
                continue

            if "nick" not in data:
                data["nick"] = None
            await self.remove_player(data["id"], data["nick"])
            print("Removing %s due to unmet conditions!" % data["id"])
            # TODO

        if not targeted_nicks:
            return

        all_online_players = await self.get_online_players()
        if not all_online_players:
            return

        for nick in targeted_nicks:
            if nick in all_online_players and nick not in self.online_players:
                self.online_players.add(nick)
                print("%s joined the game!" % nick)
                # TODO

        for nick in copy(self.online_players):
            if nick not in all_online_players:
                self.online_players.remove(nick)
                print("%s left the game!" % nick)
                # TODO

    async def start_listener(self, handler) -> None:
        """
        Start listening for and handling user inputs
        :param handler: outer function handling unexpected API behaviour
        """
        while True:
            try:
                field = await aioconsole.ainput()
                await self.handle_input(field.strip())
            except ResponseError as error:
                await handler(error)

    async def start_looper(self, handler) -> None:
        """
        Start the loop of repeating lookups
        :param handler: outer function handling unexpected API behaviour
        """
        while True:
            try:
                await self.execute()
            except ResponseError as error:
                await handler(error)
            await asyncio.sleep(self.interval)


async def handle_response_error(error):
    print(error)
    print(
        "Response Body: %s"
        % (
            error.response["error"]
            if error.reference == "BAD_INTERNAL_STATUS"
            else error.response
        )
    )


async def main():
    announcer = Announcer(filename="players.txt", server="sur", interval=15)

    li = asyncio.ensure_future(announcer.start_listener(handle_response_error))
    lo = asyncio.ensure_future(announcer.start_looper(handle_response_error))

    await li
    await lo


if __name__ == "__main__":
    try:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        pass
