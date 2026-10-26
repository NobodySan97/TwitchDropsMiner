from __future__ import annotations

import re
import json
import random
import asyncio
import logging
from time import time
from copy import deepcopy
from itertools import chain
from functools import partial
from collections import abc, deque, OrderedDict, defaultdict
from datetime import datetime, timedelta, timezone
from contextlib import suppress, asynccontextmanager
from typing import Any, Literal, Final, NoReturn, overload, cast, TYPE_CHECKING

import aiohttp
from yarl import URL

from translate import _
from gui import GUIManager
from channel import Channel
from websocket import WebsocketPool
from inventory import DropsCampaign
from exceptions import (
    ExitRequest,
    GQLException,
    ReloadRequest,
    RequestException,
)
from utils import (
    CHARS_HEX_LOWER,
    chunk,
    timestamp,
    create_nonce,
    task_wrapper,
    first_to_complete,
    RateLimiter,
    AwaitableValue,
    ExponentialBackoff,
)
from constants import (
    CALL,
    MAX_INT,
    DUMP_PATH,
    COOKIES_PATH,
    MAX_CHANNELS,
    GQL_QUERIES,
    WATCH_INTERVAL,
    State,
    ClientType,
    PriorityMode,
    WebsocketTopic,
)


if TYPE_CHECKING:
    from utils import Game
    from gui import LoginForm
    from channel import Stream
    from settings import Settings
    from inventory import TimedDrop
    from constants import ClientInfo, JsonType, GQLOperation


logger = logging.getLogger("TwitchDrops")
gql_logger = logging.getLogger("TwitchDrops.gql")


class _AuthState:
    def __init__(self, twitch: Twitch):
        self._twitch: Twitch = twitch
        self._lock = asyncio.Lock()
        self._logged_in = asyncio.Event()
        self.user_id: int
        self.user_login: str
        self.device_id: str
        self.session_id: str
        self.access_token: str
        self.client_version: str

    def _hasattrs(self, *attrs: str) -> bool:
        return all(hasattr(self, attr) for attr in attrs)

    def _delattrs(self, *attrs: str) -> None:
        for attr in attrs:
            if hasattr(self, attr):
                delattr(self, attr)

    def invalidate(self, *, delete_cookies: bool = False) -> None:
        self._delattrs("access_token", "user_id", "user_login")
        self._logged_in.clear()
        self._twitch.gui.help._invalidate_button.config(state="disabled")
        session = self._twitch._session
        if delete_cookies:
            if session is not None:
                jar = cast(aiohttp.CookieJar, session.cookie_jar)
                jar.clear()
            COOKIES_PATH.unlink(missing_ok=True)
        elif session is not None:
            client_info = self._twitch._client_type
            if client_info.CLIENT_URL.host:
                jar = cast(aiohttp.CookieJar, session.cookie_jar)
                jar.clear_domain(client_info.CLIENT_URL.host)

    def clear(self) -> None:
        self._delattrs(
            "user_id",
            "user_login",
            "device_id",
            "session_id",
            "access_token",
            "client_version",
        )
        self._logged_in.clear()
        self._twitch.gui.help._invalidate_button.config(state="disabled")

    async def _browser_login(self) -> str:
        import zendriver

        gui_print = self._twitch.gui.print
        login_form: LoginForm = self._twitch.gui.login
        client_info: ClientInfo = self._twitch._client_type
        coro_unless_closed = self._twitch.gui.coro_unless_closed

        if client_info.CLIENT_ID != ClientType.ANDROID_APP.CLIENT_ID:
            raise RuntimeError("Browser login has only been tested for the ANDROID_APP client")

        while True:
            gui_print(_("login", "browser", "startup"))
            browser: zendriver.Browser | None = None
            try:
                browser = await zendriver.start()
                await browser.get(
                    f"https://www.twitch.tv/login?client_id={client_info.CLIENT_ID}&"
                    f"redirect_params=client_id%3D{client_info.CLIENT_ID}%26"
                    "redirect_uri%3Dhttps%253A%252F%252Fwww.twitch.tv%252F%26response_type%3Dtoken"
                )

                async def url_waiter(browser=browser):
                    while await browser.test_connection():
                        tabs = getattr(browser, "tabs", [browser.main_tab])
                        for tab in tabs:
                            if tab is not None and "#access_token=" in str(tab.url):
                                return
                        await asyncio.sleep(0.5)

                gui_print(_("login", "browser", "login_to_complete"))
                await first_to_complete([
                    url_waiter(),
                    coro_unless_closed(login_form.wait_for_login_press()),
                ])

                # check if the browser window is still open and check for token across open tabs
                if await browser.test_connection():
                    token = None
                    tabs = getattr(browser, "tabs", [browser.main_tab])
                    for tab in tabs:
                        if tab is not None:
                            match = re.search(r'#access_token=(\w+)', str(tab.url), re.I)
                            if match is not None:
                                token = match.group(1)
                                break
                    if token is not None:
                        return token
                    gui_print(_("login", "browser", "no_token"))
                else:
                    gui_print(_("login", "browser", "closed_window"))
            except (ExitRequest, asyncio.CancelledError):
                raise
            except Exception as b_err:
                logger.error(f"Error during browser login: {b_err}")
            finally:
                if browser is not None:
                    try:
                        await asyncio.wait_for(browser.stop(), timeout=3.0)
                    except Exception:
                        pass
                    browser = None
            await coro_unless_closed(login_form.wait_for_login_press())

    def headers(self, *, user_agent: str = '', gql: bool = False, authenticated: bool = True) -> JsonType:
        client_info: ClientInfo = self._twitch._client_type
        headers = {
            "Accept": "*/*",
            "Accept-Encoding": "gzip",
            "Accept-Language": "en-US",
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
            "Client-Id": client_info.CLIENT_ID,
        }
        if user_agent:
            headers["User-Agent"] = user_agent
        if hasattr(self, "session_id"):
            headers["Client-Session-Id"] = self.session_id
        if hasattr(self, "device_id"):
            headers["X-Device-Id"] = self.device_id
        if gql:
            headers["Origin"] = str(client_info.CLIENT_URL)
            headers["Referer"] = str(client_info.CLIENT_URL)
            if authenticated and hasattr(self, "access_token"):
                headers["Authorization"] = f"OAuth {self.access_token}"
        return headers

    async def validate(self):
        async with self._lock:
            await self._validate()

    async def _validate(self):
        if not hasattr(self, "session_id"):
            self.session_id = create_nonce(CHARS_HEX_LOWER, 16)
        if not self._hasattrs("device_id", "access_token", "user_id"):
            session = await self._twitch.get_session()
            jar = cast(aiohttp.CookieJar, session.cookie_jar)
            client_info: ClientInfo = self._twitch._client_type
        if not self._hasattrs("device_id"):
            cookie = jar.filter_cookies(client_info.CLIENT_URL)
            if "unique_id" in cookie:
                self.device_id = cookie["unique_id"].value
            else:
                self.device_id = create_nonce(CHARS_HEX_LOWER, 32)
                cookie["unique_id"] = self.device_id
                jar.update_cookies(cookie, client_info.CLIENT_URL)
        if not self._hasattrs("access_token", "user_id"):
            # looks like we're missing something
            login_form: LoginForm = self._twitch.gui.login
            logger.info("Checking login")
            login_form.update(_("gui", "login", "logging_in"), None)
            for client_mismatch_attempt in range(2):
                for invalid_token_attempt in range(2):
                    cookie = jar.filter_cookies(client_info.CLIENT_URL)
                    if "auth-token" not in cookie:
                        self.access_token = await self._browser_login()
                        cookie["auth-token"] = self.access_token
                    elif not hasattr(self, "access_token"):
                        logger.info("Restoring session from cookie")
                        self.access_token = cookie["auth-token"].value
                    # validate the auth token, by obtaining user_id
                    async with self._twitch.request(
                        "GET",
                        "https://id.twitch.tv/oauth2/validate",
                        headers={"Authorization": f"OAuth {self.access_token}"}
                    ) as response:
                        if response.status == 401:
                            # the access token we have is invalid - clear the cookie and reauth
                            logger.info("Restored session is invalid")
                            assert client_info.CLIENT_URL.host is not None
                            jar.clear_domain(client_info.CLIENT_URL.host)
                            continue
                        elif response.status == 200:
                            validate_response = await response.json()
                            break
                else:
                    raise RuntimeError("Login verification failure (step #2)")
                # ensure the cookie's client ID matches the currently selected client
                if validate_response["client_id"] == client_info.CLIENT_ID:
                    break
                # otherwise, we need to delete the entire cookie file and clear the jar
                logger.info("Cookie client ID mismatch")
                jar.clear()
                COOKIES_PATH.unlink(missing_ok=True)
            else:
                raise RuntimeError("Login verification failure (step #1)")
            self.user_id = int(validate_response["user_id"])
            self.user_login = str(validate_response.get("login", ""))
            cookie["persistent"] = str(self.user_id)
            logger.info(f"Login successful, user ID: {self.user_id}")
            login_form.update(_("gui", "login", "logged_in"), self.user_id)
            # update our cookie and save it
            jar.update_cookies(cookie, client_info.CLIENT_URL)
            jar.save(COOKIES_PATH)
        if hasattr(self, "user_id"):
            self._twitch.gui.login.update(_("gui", "login", "logged_in"), self.user_id)
        self._twitch.gui.help._invalidate_button.config(state="normal")
        self._logged_in.set()


class Twitch:
    def __init__(self, settings: Settings):
        self.settings: Settings = settings
        # State management
        self._state: State = State.IDLE
        self._state_change = asyncio.Event()
        self.wanted_games: list[Game] = []
        self.inventory: list[DropsCampaign] = []
        self._known_discovered_campaigns: dict[str, JsonType] = {}
        self._resolved_game_slugs: dict[str, str] = {}
        self._drops: dict[str, TimedDrop] = {}
        self._campaigns: dict[str, DropsCampaign] = {}
        self._claimed_drops: set[str] = set()
        self._mnt_triggers: deque[datetime] = deque()
        # NOTE: GQL is pretty volatile and breaks everything if one runs into their rate limit.
        # Do not modify the default, safe values.
        self._gql_limiter = RateLimiter(capacity=5, window=1)
        # Client type, session and auth
        self._client_type: ClientInfo = ClientType.ANDROID_APP
        self._session: aiohttp.ClientSession | None = None
        self._auth_state: _AuthState = _AuthState(self)
        # GUI
        self.gui = GUIManager(self)
        # Storing and watching channels
        self.channels: OrderedDict[int, Channel] = OrderedDict()
        self.watching_channel: AwaitableValue[Channel] = AwaitableValue()
        self._watching_task: asyncio.Task[None] | None = None
        self._watching_restart = asyncio.Event()
        # Websocket
        self.websocket = WebsocketPool(self)
        # Maintenance task
        self._mnt_task: asyncio.Task[None] | None = None
        self._retry_task: asyncio.Task[None] | None = None
        self._last_gql_op: str = "None"
        self._stalled_channels: dict[int, datetime] = {}
        self._channel_last_progress: dict[int, float] = {}

    async def get_session(self) -> aiohttp.ClientSession:
        if (session := self._session) is not None:
            if session.closed:
                raise RuntimeError("Session is closed")
            return session
        # load in cookies
        cookie_jar = aiohttp.CookieJar()
        try:
            if COOKIES_PATH.exists():
                cookie_jar.load(COOKIES_PATH)
        except Exception:
            # if loading in the cookies file ends up in an error, just ignore it
            # clear the jar, just in case
            cookie_jar.clear()
            COOKIES_PATH.unlink(missing_ok=True)
        # create timeouts
        # connection quality mulitiplier determines the magnitude of timeouts
        connection_quality = self.settings.connection_quality
        if connection_quality < 1:
            connection_quality = self.settings.connection_quality = 1
        elif connection_quality > 6:
            connection_quality = self.settings.connection_quality = 6
        timeout = aiohttp.ClientTimeout(
            sock_connect=5*connection_quality,
            total=10*connection_quality,
        )
        # create session, limited to 50 connections at maximum
        connector = aiohttp.TCPConnector(limit=50, keepalive_timeout=60.0)
        session_kwargs: dict[str, Any] = {
            "timeout": timeout,
            "connector": connector,
            "cookie_jar": cookie_jar,
            "headers": {"User-Agent": self._client_type.USER_AGENT},
            "max_line_size": 65536,
            "max_field_size": 65536,
        }
        try:
            self._session = aiohttp.ClientSession(**session_kwargs)
        except TypeError:
            session_kwargs.pop("max_line_size", None)
            session_kwargs.pop("max_field_size", None)
            self._session = aiohttp.ClientSession(**session_kwargs)
        return self._session

    async def shutdown(self) -> None:
        start_time = time()
        self.stop_watching()
        if self._retry_task is not None:
            self._retry_task.cancel()
            self._retry_task = None
        for task in (self._watching_task, self._mnt_task):
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        self._watching_task = None
        self._mnt_task = None
        # stop websocket, close session and save cookies
        await self.websocket.stop(clear_topics=True)
        if self._session is not None:
            cookie_jar = cast(aiohttp.CookieJar, self._session.cookie_jar)
            # clear empty cookie entries off the cookies file before saving
            # NOTE: Unfortunately, aiohttp provides no easy way of clearing empty cookies,
            # so we need to access the private '_cookies' attribute for this.
            for cookie_key, cookie in list(cookie_jar._cookies.items()):
                if not cookie:
                    del cookie_jar._cookies[cookie_key]
            cookie_jar.save(COOKIES_PATH)
            await self._session.close()
            self._session = None
        self._drops.clear()
        for channel in self.channels.values():
            channel.remove()
        self.channels.clear()
        self.inventory.clear()
        self._auth_state.clear()
        self.wanted_games.clear()
        self._mnt_triggers.clear()
        self._claimed_drops.clear()
        self._channel_last_progress.clear()
        # wait at least half a second + whatever it takes to complete the closing
        # this allows aiohttp to safely close the session
        await asyncio.sleep(start_time + 0.5 - time())

    def wait_until_login(self) -> abc.Coroutine[Any, Any, Literal[True]]:
        return self._auth_state._logged_in.wait()

    def change_state(self, state: State) -> None:
        if self._state is State.EXIT or (self._state is State.RESTART and state is not State.EXIT):
            return
        if state is not State.IDLE and self._retry_task is not None and not self._retry_task.done():
            self._retry_task.cancel()
            self._retry_task = None
        self._state = state
        self._state_change.set()

    def state_change(self, state: State) -> abc.Callable[[], None]:
        # this is identical to change_state, but defers the call
        # perfect for GUI usage
        return partial(self.change_state, state)

    def close(self):
        """
        Called when the application is requested to close by the user,
        usually by the console or application window being closed.
        """
        self.change_state(State.EXIT)

    def prevent_close(self):
        """
        Called when the application window has to be prevented from closing, even after the user
        closes it with X. Usually used solely to display tracebacks from the closing sequence.
        """
        self.gui.prevent_close()

    def print(self, message: str):
        """
        Can be used to print messages within the GUI.
        """
        self.gui.print(message)

    def save(self, *, force: bool = False) -> None:
        """
        Saves the application state.
        """
        self.gui.save(force=force)
        self.settings.save(force=force)

    def get_priority(self, channel: Channel) -> int:
        """
        Return a priority number for a given channel.

        0 has the highest priority.
        Higher numbers -> lower priority.
        MAX_INT (a really big number) signifies the lowest possible priority.
        """
        if (
            (game := channel.game) is None  # None when OFFLINE or no game set
            or game not in self.wanted_games  # we don't care about the played game
        ):
            return MAX_INT
        return self.wanted_games.index(game)

    @staticmethod
    def _viewers_key(channel: Channel) -> int:
        if (viewers := channel.viewers) is not None:
            return viewers
        return -1

    async def run(self):
        if self.settings.dump:
            with open(DUMP_PATH, 'w', encoding="utf8"):
                # replace the existing file with an empty one
                pass
        while True:
            try:
                await self._run()
                break
            except ReloadRequest:
                await self.shutdown()
            except ExitRequest:
                break
            except aiohttp.ContentTypeError as exc:
                raise RequestException(_("login", "unexpected_content")) from exc

    async def _run(self):
        """
        Main method that runs the whole client.

        Here, we manage several things, specifically:
        • Fetching the drops inventory to make sure that everything we can claim, is claimed
        • Selecting a stream to watch, and watching it
        • Changing the stream that's being watched if necessary
        """
        self.gui.start()
        auth_state = await self.get_auth()
        await self.websocket.start()
        # NOTE: watch task is explicitly restarted on each new run
        if self._watching_task is not None:
            self._watching_task.cancel()
        self._watching_task = asyncio.create_task(self._watch_loop())
        # Add default topics
        self.websocket.add_topics([
            WebsocketTopic("User", "Drops", auth_state.user_id, self.process_drops),
            WebsocketTopic(
                "User", "Notifications", auth_state.user_id, self.process_notifications
            ),
        ])
        full_cleanup: bool = False
        channels: Final[OrderedDict[int, Channel]] = self.channels
        self.change_state(State.INVENTORY_FETCH)
        while True:
            if self._state is State.IDLE:
                if self.settings.dump:
                    self.gui.close()
                    continue
                self.gui.tray.change_icon("idle")
                self.gui.status.update(_("gui", "status", "idle"))
                self.stop_watching()
                # clear the flag and wait until it's set again
                self._state_change.clear()
            elif self._state is State.INVENTORY_FETCH:
                self.gui.tray.change_icon("maint")
                # ensure the websocket is running
                await self.websocket.start()
                await self.fetch_inventory()
                self.gui.set_games(set(campaign.game for campaign in self.inventory))
                # Save state on every inventory fetch
                self.save()
                self.change_state(State.GAMES_UPDATE)
            elif self._state is State.GAMES_UPDATE:
                # claim drops from expired and active campaigns
                for campaign in self.inventory:
                    if not campaign.upcoming:
                        for drop in campaign.drops:
                            if drop.can_claim:
                                await drop.claim()
                # figure out which games we want
                self.wanted_games.clear()
                exclude = self.settings.exclude
                priority = self.settings.priority
                priority_mode = self.settings.priority_mode
                priority_only = priority_mode is PriorityMode.PRIORITY_ONLY
                next_hour = datetime.now(timezone.utc) + timedelta(hours=1)
                sorted_campaigns: list[DropsCampaign] = list(self.inventory)
                if not priority_only:
                    if priority_mode is PriorityMode.ENDING_SOONEST:
                        sorted_campaigns.sort(key=lambda c: c.ends_at)
                    elif priority_mode is PriorityMode.LOW_AVBL_FIRST:
                        sorted_campaigns.sort(key=lambda c: c.availability)
                    elif priority_mode is PriorityMode.SHORTEST_TIME_FIRST:
                        sorted_campaigns.sort(
                            key=lambda c: (
                                c.first_drop.remaining_minutes
                                if c.first_drop
                                else c.remaining_minutes
                            )
                        )
                sorted_campaigns.sort(
                    key=lambda c: (
                        priority.index(c.game.name) if c.game.name in priority else MAX_INT
                    )
                )
                for campaign in sorted_campaigns:
                    game: Game = campaign.game
                    if (
                        game not in self.wanted_games  # isn't already there
                        # and isn't excluded by list or priority mode
                        and game.name not in exclude
                        and (not priority_only or game.name in priority)
                        # and can be progressed within the next hour
                        and campaign.can_earn_within(next_hour)
                    ):
                        # non-excluded games with no priority are placed last, below priority ones
                        self.wanted_games.append(game)
                full_cleanup = True
                self.restart_watching()
                self.change_state(State.CHANNELS_CLEANUP)
            elif self._state is State.CHANNELS_CLEANUP:
                self.gui.status.update(_("gui", "status", "cleanup"))
                if not self.wanted_games or full_cleanup:
                    # no games selected or we're doing full cleanup: remove everything
                    to_remove_channels: list[Channel] = list(channels.values())
                else:
                    # remove all channels that:
                    to_remove_channels = [
                        channel
                        for channel in channels.values()
                        if (
                            not channel.acl_based  # aren't ACL-based
                            and (
                                channel.offline  # and are offline
                                # or online but aren't streaming the game we want anymore
                                or (channel.game is None or channel.game not in self.wanted_games)
                            )
                        )
                    ]
                full_cleanup = False
                if to_remove_channels:
                    to_remove_topics: list[str] = []
                    for channel in to_remove_channels:
                        to_remove_topics.append(
                            WebsocketTopic.as_str("Channel", "StreamState", channel.id)
                        )
                        to_remove_topics.append(
                            WebsocketTopic.as_str("Channel", "StreamUpdate", channel.id)
                        )
                    self.websocket.remove_topics(to_remove_topics)
                    for channel in to_remove_channels:
                        del channels[channel.id]
                        channel.remove()
                    del to_remove_channels, to_remove_topics
                if self.wanted_games:
                    self.change_state(State.CHANNELS_FETCH)
                else:
                    # with no games available, we switch to IDLE after cleanup
                    self.print(_("status", "no_campaign"))
                    self.change_state(State.IDLE)
            elif self._state is State.CHANNELS_FETCH:
                self.gui.status.update(_("gui", "status", "gathering"))
                # start with all current channels, clear the memory and GUI
                new_channels: set[Channel] = set(channels.values())
                channels.clear()
                self.gui.channels.clear()
                # gather and add ACL channels from campaigns
                # NOTE: we consider only campaigns that can be progressed
                # NOTE: we use another set so that we can set them online separately
                no_acl: set[Game] = set()
                acl_channels: set[Channel] = set()
                next_hour = datetime.now(timezone.utc) + timedelta(hours=1)
                for campaign in self.inventory:
                    if (
                        campaign.game in self.wanted_games
                        and campaign.can_earn_within(next_hour)
                    ):
                        if campaign.allowed_channels:
                            acl_channels.update(campaign.allowed_channels)
                        else:
                            no_acl.add(campaign.game)
                # remove all ACL channels that already exist from the other set
                acl_channels.difference_update(new_channels)
                # use the other set to set them online if possible
                await self.bulk_check_online(acl_channels)
                # finally, add them as new channels
                new_channels.update(acl_channels)
                ordered_no_acl = sorted(
                    [g for g in no_acl if g and g.slug],
                    key=lambda g: self.wanted_games.index(g) if g in self.wanted_games else 999
                )
                if ordered_no_acl:
                    stream_results = await asyncio.gather(
                        *(self.get_live_streams(game, drops_enabled=True) for game in ordered_no_acl),
                        return_exceptions=True
                    )
                    dir_channels: list[Channel] = []
                    for res in stream_results:
                        if isinstance(res, list):
                            dir_channels.extend(res)
                            new_channels.update(res)
                    if self.settings.available_drops_check and dir_channels:
                        await self.bulk_check_available_drops(dir_channels)
                # sort them descending by viewers, by priority and by game priority
                # NOTE: Viewers sort also ensures ONLINE channels are sorted to the top
                # NOTE: We can drop using the set now, because there's no more channels being added
                ordered_channels: list[Channel] = sorted(
                    new_channels, key=self._viewers_key, reverse=True
                )
                ordered_channels.sort(key=lambda ch: ch.acl_based, reverse=True)
                ordered_channels.sort(key=self.get_priority)
                # ensure that we won't end up with more channels than we can handle
                # NOTE: we trim from the end because that's where the non-priority,
                # offline (or online but low viewers) channels end up
                to_remove_channels = ordered_channels[MAX_CHANNELS:]
                ordered_channels = ordered_channels[:MAX_CHANNELS]
                if to_remove_channels:
                    # tracked channels and gui were cleared earlier, so no need to do it here
                    # just make sure to unsubscribe from their topics
                    to_remove_topics = []
                    for channel in to_remove_channels:
                        to_remove_topics.append(
                            WebsocketTopic.as_str("Channel", "StreamState", channel.id)
                        )
                        to_remove_topics.append(
                            WebsocketTopic.as_str("Channel", "StreamUpdate", channel.id)
                        )
                        channel.remove()
                    self.websocket.remove_topics(to_remove_topics)
                    del to_remove_channels, to_remove_topics
                # set our new channel list
                for channel in ordered_channels:
                    channels[channel.id] = channel
                    channel.display(add=True)
                # subscribe to these channel's state updates
                to_add_topics: list[WebsocketTopic] = []
                for channel_id in channels:
                    to_add_topics.append(
                        WebsocketTopic(
                            "Channel", "StreamState", channel_id, self.process_stream_state
                        )
                    )
                    to_add_topics.append(
                        WebsocketTopic(
                            "Channel", "StreamUpdate", channel_id, self.process_stream_update
                        )
                    )
                self.websocket.add_topics(to_add_topics)
                # relink watching channel after cleanup,
                # or stop watching it if it no longer qualifies
                # NOTE: this replaces 'self.watching_channel's internal value with the new object
                watching_channel = self.watching_channel.get_with_default(None)
                if watching_channel is not None:
                    new_watching: Channel | None = channels.get(watching_channel.id)
                    if new_watching is not None and self.can_watch(new_watching):
                        self.watch(new_watching, update_status=False)
                    else:
                        # we've removed a channel we were watching
                        self.stop_watching()
                    del new_watching
                # pre-display the active drop with a substracted minute
                for channel in channels.values():
                    # check if there's any channels we can watch first
                    if self.can_watch(channel):
                        if (
                            (active_campaign := self.get_active_campaign(channel)) is not None
                            and (active_drop := active_campaign.first_drop) is not None
                        ):
                            active_drop.display(countdown=False, subone=True)
                        break
                self.change_state(State.CHANNEL_SWITCH)
                del (
                    no_acl,
                    acl_channels,
                    new_channels,
                    to_add_topics,
                    ordered_channels,
                    watching_channel,
                )
            elif self._state is State.CHANNEL_SWITCH:
                if self.settings.dump:
                    self.gui.close()
                    continue
                self.gui.status.update(_("gui", "status", "switching"))
                # Change into the selected channel, stay in the watching channel,
                # or select a new channel that meets the required conditions
                new_watching = None
                selected_channel = self.gui.channels.get_selection()
                if selected_channel is not None and self.can_watch(selected_channel):
                    # selected channel is checked first, and set as long as we can watch it
                    new_watching = selected_channel
                else:
                    # other channels additionally need to have a good reason
                    # for a switch (including the watching one)
                    # NOTE: we need to sort the channels every time because one channel
                    # can end up streaming any game - channels aren't game-tied
                    for channel in sorted(channels.values(), key=self.get_priority):
                        if self.should_switch(channel):
                            new_watching = channel
                            break
                watching_channel = self.watching_channel.get_with_default(None)
                if new_watching is not None:
                    # if we have a better switch target - do so
                    self.watch(new_watching)
                    # break the state change chain by clearing the flag
                    self._state_change.clear()
                elif watching_channel is not None and self.can_watch(watching_channel):
                    # otherwise, continue watching what we had before
                    self.gui.status.update(
                        _("status", "watching").format(channel=watching_channel.name)
                    )
                    # break the state change chain by clearing the flag
                    self._state_change.clear()
                else:
                    # not watching anything and there isn't anything to watch either
                    self.print(_("status", "no_channel"))
                    if self._retry_task is not None and not self._retry_task.done():
                        self._retry_task.cancel()
                    if self.wanted_games:
                        async def _retry_fetch():
                            await asyncio.sleep(60)
                            if self._state is State.IDLE and self.wanted_games:
                                self.change_state(State.CHANNELS_FETCH)
                        self._retry_task = asyncio.create_task(_retry_fetch())
                    self.change_state(State.IDLE)
                del new_watching, selected_channel, watching_channel
            elif self._state is State.RESTART:
                raise ReloadRequest()
            elif self._state is State.EXIT:
                self.gui.tray.change_icon("pickaxe")
                self.gui.status.update(_("gui", "status", "exiting"))
                # we've been requested to exit the application
                break
            await self._state_change.wait()

    async def _watch_sleep(self, delay: float) -> None:
        if self._watching_restart.is_set():
            return
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._watching_restart.wait(), timeout=delay)

    @task_wrapper(critical=True)
    async def _watch_loop(self) -> NoReturn:
        interval: float = WATCH_INTERVAL.total_seconds()
        watch_failures: int = 0
        while True:
            if self._watching_restart.is_set():
                self._watching_restart.clear()
                watch_failures = 0
            channel: Channel = await self.watching_channel.get()
            if not channel.online:
                # if the channel isn't online anymore, we stop watching it
                self.stop_watching()
                self.change_state(State.CHANNEL_SWITCH)
                continue
            # logger.log(CALL, f"Sending watch payload to: {channel.name}")
            last_sent: float = time()
            try:
                succeeded: bool = await channel.send_watch()
            except Exception as exc:
                logger.debug(f"Unhandled error in send_watch for {channel.name}: {exc}")
                succeeded = False
            if not succeeded:
                watch_failures += 1
                logger.log(CALL, f"Watch requested failed for channel: {channel.name} (streak: {watch_failures})")
                if not channel.online or watch_failures >= 3:
                    if watch_failures >= 3:
                        logger.warning(
                            f"Watch failed {watch_failures} consecutive times on {channel.name}. Penalizing channel."
                        )
                        self.penalize_channel(channel)
                    self.stop_watching()
                    self.change_state(State.CHANNEL_SWITCH)
                    continue
            else:
                watch_failures = 0
                if not self.gui.progress.timer_running:
                    if (
                        (active_campaign := self.get_active_campaign(channel)) is not None
                        and (active_drop := active_campaign.first_drop) is not None
                    ):
                        active_drop.display(countdown=True)
            # wait ~15 seconds for a progress update (interruptible if channel switched)
            await self._watch_sleep(15 - min(time() - last_sent, 15))
            if self._watching_restart.is_set():
                continue
            if succeeded and self.gui.progress.minute_almost_done():
                try:
                    await self.check_drop_progress(channel)
                except Exception as exc:
                    logger.debug(f"Error checking drop progress: {exc}")
            jitter = random.uniform(1.0, 3.0)
            sleep_duration = max(0.0, (interval + jitter) - (time() - last_sent))
            await self._watch_sleep(sleep_duration)

    async def check_drop_progress(self, channel: Channel | None = None) -> bool:
        if channel is None:
            channel = self.watching_channel.get_with_default(None)
        if channel is None:
            return False

        try:
            context = await self.gql_request(
                GQL_QUERIES["CurrentDrop"].with_variables(
                    {"channelID": str(channel.id)}
                )
            )
            user_data = (context.get("data") or {}).get("currentUser")
            drop_data: JsonType | None = (
                user_data.get("dropCurrentSession")
                if isinstance(user_data, dict)
                else None
            )
        except RequestException:
            drop_data = None
        if drop_data is not None:
            gql_drop: TimedDrop | None = self._drops.get(drop_data["dropID"])
            if gql_drop is not None and gql_drop.can_earn(channel):
                new_minutes = drop_data["currentMinutesWatched"]
                if new_minutes > gql_drop.real_current_minutes:
                    gql_drop.update_minutes(new_minutes)
                    self._channel_last_progress[channel.id] = time()
                    self._stalled_channels.pop(channel.id, None)
                    drop_text: str = (
                        f"{gql_drop.name} ({gql_drop.campaign.game}, "
                        f"{gql_drop.current_minutes}/{gql_drop.required_minutes})"
                    )
                    logger.log(CALL, f"Drop progress from GQL: {drop_text}")
                    return True

        # Check if drop progress has stalled on this channel for more than 4 minutes (240s)
        last_progress = self._channel_last_progress.setdefault(channel.id, time())
        elapsed = time() - last_progress
        if elapsed >= 240.0:
            logger.warning(
                f"Drop progress on channel {channel.name} has not advanced for {int(elapsed // 60)} minutes. Penalizing channel."
            )
            self._channel_last_progress.pop(channel.id, None)
            self.penalize_channel(channel)
            self.stop_watching()
            self.change_state(State.CHANNEL_SWITCH)
        return False

    @task_wrapper(critical=True)
    async def _maintenance_task(self) -> None:
        now = datetime.now(timezone.utc)
        next_period = now + timedelta(hours=1)
        while True:
            # exit if there's no need to repeat the loop
            now = datetime.now(timezone.utc)
            if now >= next_period:
                break
            next_trigger = next_period
            while self._mnt_triggers and self._mnt_triggers[0] <= next_trigger:
                next_trigger = self._mnt_triggers.popleft()
            trigger_type: str = "Reload" if next_trigger == next_period else "Cleanup"
            logger.log(
                CALL,
                (
                    "Maintenance task waiting until: "
                    f"{next_trigger.astimezone().strftime('%X')} ({trigger_type})"
                )
            )
            await asyncio.sleep((next_trigger - now).total_seconds())
            # exit after waiting, before the actions
            now = datetime.now(timezone.utc)
            if now >= next_period:
                break
            if next_trigger != next_period:
                logger.log(CALL, "Maintenance task requests channels cleanup")
                self.change_state(State.CHANNELS_CLEANUP)
        # this triggers a restart of this task every (up to) 60 minutes
        logger.log(CALL, "Maintenance task requests a reload")
        self.change_state(State.INVENTORY_FETCH)

    def penalize_channel(self, channel: Channel, duration: timedelta = timedelta(minutes=30)) -> None:
        self._stalled_channels[channel.id] = datetime.now(timezone.utc) + duration
        logger.info(
            f"Channel {channel.name} penalized for {int(duration.total_seconds() / 60)} "
            "minutes due to stalled drop progress"
        )

    def can_watch(self, channel: Channel) -> bool:
        """
        Determines if the given channel qualifies as a watching candidate.
        """
        # exit early if stream is offline
        if not channel.online:
            return False
        if channel.id in self._stalled_channels:
            if datetime.now(timezone.utc) < self._stalled_channels[channel.id]:
                return False
            del self._stalled_channels[channel.id]
        for campaign in self.inventory:
            if (
                campaign.can_earn(channel)
                and (
                    # limit watching to the games the user wants
                    channel.game is not None
                    and channel.drops_enabled
                    and channel.game in self.wanted_games
                    # or an explicit ACL channel whitelisted for this campaign
                    or bool(campaign.allowed_channels and channel in campaign.allowed_channels)
                )
            ):
                return True
        return False

    def should_switch(self, channel: Channel) -> bool:
        """
        Determines if the given channel qualifies as a switch candidate.
        """
        if not self.can_watch(channel):
            return False
        watching_channel = self.watching_channel.get_with_default(None)
        if watching_channel is None or not self.can_watch(watching_channel):
            return True
        channel_order = self.get_priority(channel)
        watching_order = self.get_priority(watching_channel)
        return (
            # this channel's game is higher order than the watching one's
            channel_order < watching_order
            or channel_order == watching_order  # or the order is the same
            # and this channel is ACL-based and the watching channel isn't
            and channel.acl_based > watching_channel.acl_based
        )

    def watch(self, channel: Channel, *, update_status: bool = True):
        prev_channel = self.watching_channel.get_with_default(None)
        self.gui.tray.change_icon("active")
        self.gui.channels.set_watching(channel)
        self.watching_channel.set(channel)
        if update_status:
            status_text = _("status", "watching").format(channel=channel.name)
            self.print(status_text)
            self.gui.status.update(status_text)
        if prev_channel is None or prev_channel != channel:
            self._channel_last_progress[channel.id] = time()
            self.restart_watching()

    def stop_watching(self):
        watching = self.watching_channel.get_with_default(None)
        if watching is not None:
            self._channel_last_progress.pop(watching.id, None)
        self.gui.clear_drop()
        self.watching_channel.clear()
        self.gui.channels.clear_watching()
        self.restart_watching()

    def restart_watching(self):
        self.gui.progress.stop_timer()
        self._watching_restart.set()

    @task_wrapper
    async def process_stream_state(self, channel_id: int, message: JsonType):
        msg_type = message["type"]
        channel = self.channels.get(channel_id)
        if channel is None:
            logger.error(f"Stream state change for a non-existing channel: {channel_id}")
            return
        if msg_type == "viewcount":
            if not channel.online:
                # if it's not online for some reason, set it so
                channel.check_online()
            else:
                viewers = message["viewers"]
                channel.viewers = viewers
                channel.display()
                # logger.debug(f"{channel.name} viewers: {viewers}")
        elif msg_type == "stream-down":
            channel.set_offline()
        elif msg_type == "stream-up":
            channel.check_online()
        elif msg_type == "commercial":
            # skip these
            pass
        else:
            logger.warning(f"Unknown stream state: {msg_type}")

    @task_wrapper
    async def process_stream_update(self, channel_id: int, message: JsonType):
        # message = {
        #     "channel_id": "12345678",
        #     "type": "broadcast_settings_update",
        #     "channel": "channel._login",
        #     "old_status": "Old title",
        #     "status": "New title",
        #     "old_game": "Old game name",
        #     "game": "New game name",
        #     "old_game_id": 123456,
        #     "game_id": 123456
        # }
        channel = self.channels.get(channel_id)
        if channel is None:
            logger.error(f"Broadcast settings update for a non-existing channel: {channel_id}")
            return
        if message["old_game"] != message["game"]:
            game_change = f", game changed: {message['old_game']} -> {message['game']}"
        else:
            game_change = ''
        logger.log(CALL, f"Channel update from websocket: {channel.name}{game_change}")
        # There's no information about channel tags here, but this event is triggered
        # when the tags change. We can use this to just update the stream data after the change.
        # Use 'check_online' to introduce a delay, allowing for multiple title and tags
        # changes before we update. This eventually calls 'on_channel_update' below.
        channel.check_online(delay=5.0)

    def on_channel_update(
        self, channel: Channel, stream_before: Stream | None, stream_after: Stream | None
    ):
        """
        Called by a Channel when it's status is updated (ONLINE, OFFLINE, title/tags change).

        NOTE: 'stream_before' gets dealocated once this function finishes.
        """
        if stream_before is None:
            if stream_after is not None:
                # Channel going ONLINE
                if self.should_switch(channel):
                    # we can watch the channel, and we should
                    self.print(_("status", "goes_online").format(channel=channel.name))
                    self.watch(channel)
                else:
                    logger.info(f"{channel.name} goes ONLINE")
            else:
                # Channel was OFFLINE and stays that way
                logger.log(CALL, f"{channel.name} stays OFFLINE")
        else:
            watching_channel = self.watching_channel.get_with_default(None)
            # check if the watching channel was the one updated
            if watching_channel is not None and watching_channel == channel:
                # NOTE: In these cases, channel was the watching channel
                if not self.can_watch(channel):
                    # we can't watch it anymore
                    if stream_after is None:
                        # Channel going OFFLINE
                        self.print(_("status", "goes_offline").format(channel=channel.name))
                    else:
                        # Channel stays ONLINE, but we can't watch it anymore
                        logger.info(
                            f"{channel.name} status has been updated, switching... "
                            f"(🎁: {stream_before.drops_enabled and '✔' or '❌'} -> "
                            f"{stream_after.drops_enabled and '✔' or '❌'})"
                        )
                    self.change_state(State.CHANNEL_SWITCH)
                else:
                    # Channel stays ONLINE, and we can still watch it - no change
                    pass
            # NOTE: In these cases, it wasn't the watching channel
            elif stream_after is None:
                logger.info(f"{channel.name} goes OFFLINE")
            else:
                # Channel stays ONLINE, but has been updated
                logger.info(
                    f"{channel.name} status has been updated "
                    f"(🎁: {stream_before.drops_enabled and '✔' or '❌'} -> "
                    f"{stream_after.drops_enabled and '✔' or '❌'})"
                )
                if self.should_switch(channel):
                    # ... and we should watch it
                    self.watch(channel)
        channel.display()

    @task_wrapper
    async def process_drops(self, user_id: int, message: JsonType):
        # Message examples:
        # {"type": "drop-progress", data: {"current_progress_min": 3, "required_progress_min": 10}}
        # {"type": "drop-claim", data: {"drop_instance_id": ...}}
        msg_type: str = message["type"]
        if msg_type not in ("drop-progress", "drop-claim"):
            return
        drop_id: str = message["data"]["drop_id"]
        drop: TimedDrop | None = self._drops.get(drop_id)
        watching_channel: Channel | None = self.watching_channel.get_with_default(None)
        if msg_type == "drop-claim":
            if drop is None:
                logger.error(
                    f"Received a drop claim ID for a non-existing drop: {drop_id}\n"
                    f"Drop claim ID: {message['data']['drop_instance_id']}"
                )
                return
            drop.update_claim(message["data"]["drop_instance_id"])
            drop.real_current_minutes = drop.required_minutes
            campaign = drop.campaign
            await drop.claim()
            drop.display()
            # Immediately restart watching or fetch inventory to start the next drop
            if campaign.can_earn(watching_channel):
                self.restart_watching()
            else:
                self.change_state(State.INVENTORY_FETCH)
            return
        assert msg_type == "drop-progress"
        if drop is not None:
            drop_text = (
                f"{drop.name} ({drop.campaign.game}, "
                f"{message['data']['current_progress_min']}/"
                f"{message['data']['required_progress_min']})"
            )
        else:
            drop_text = "<Unknown>"
        logger.log(CALL, f"Drop update from websocket: {drop_text}")
        if drop is not None and drop.can_earn(self.watching_channel.get_with_default(None)):
            # the received payload is for the drop we expected
            drop.update_minutes(message["data"]["current_progress_min"])
            if watching_channel is not None:
                self._channel_last_progress[watching_channel.id] = time()
                self._stalled_channels.pop(watching_channel.id, None)

    @task_wrapper
    async def process_notifications(self, user_id: int, message: JsonType):
        if message["type"] == "create-notification":
            data: JsonType = message["data"]["notification"]
            if data["type"] in (
                "user_drop_reward_reminder_notification",  # drop confirmation
                "quests_viewer_reward_campaign_earned_emote",  # emote confirmation
                # badge confirmation?
            ):
                self.change_state(State.INVENTORY_FETCH)
                await self.gql_request(
                    GQL_QUERIES["NotificationsDelete"].with_variables(
                        {"input": {"id": data["id"]}}
                    )
                )

    async def get_auth(self) -> _AuthState:
        await self._auth_state.validate()
        return self._auth_state

    @asynccontextmanager
    async def request(
        self, method: str, url: URL | str, **kwargs
    ) -> abc.AsyncIterator[aiohttp.ClientResponse]:
        session = await self.get_session()
        method = method.upper()
        if self.settings.proxy and "proxy" not in kwargs:
            kwargs["proxy"] = self.settings.proxy
        # Request options may contain login credentials, OAuth headers, or proxy credentials.
        # Keep debugging useful without writing those secrets to a console or log file.
        logger.debug("Request: method=%s, url=%s, options=%s", method, url, sorted(kwargs))
        backoff = ExponentialBackoff(maximum=3*60)
        for delay in backoff:
            if self.gui.close_requested:
                raise ExitRequest()
            try:
                response: aiohttp.ClientResponse | None = None
                response = await self.gui.coro_unless_closed(
                    session.request(method, url, **kwargs)
                )
                assert response is not None
                logger.debug(f"Response: {response.status}: {response}")
                if response.status < 500:
                    # pre-read the response to avoid getting errors outside of the context manager
                    raw_response = await response.read()  # noqa
                    yield response
                    return
                logger.warning(f"Request to {url} returned status {response.status}")
                self.print(_("error", "site_down").format(seconds=round(delay)))
            except aiohttp.ClientConnectorCertificateError:
                # for a case where SSL verification fails
                raise
            except (
                aiohttp.ClientConnectionError, asyncio.TimeoutError, aiohttp.ClientPayloadError
            ):
                # connection problems, retry
                if backoff.steps > 1:
                    # just so that quick retries that sometimes happen, aren't shown
                    self.print(
                        _("error", "no_connection").format(seconds=round(delay), url=str(url))
                    )
            finally:
                if response is not None:
                    response.release()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.gui.wait_until_closed(), timeout=delay)

    @overload
    async def gql_request(self, ops: GQLOperation, *, authenticated: bool = True) -> JsonType:
        ...

    @overload
    async def gql_request(self, ops: list[GQLOperation], *, authenticated: bool = True) -> list[JsonType]:
        ...

    async def gql_request(
        self, ops: GQLOperation | list[GQLOperation], *, authenticated: bool = True
    ) -> JsonType | list[JsonType]:
        def _get_op_name(op: Any) -> str:
            if isinstance(op, dict) and "operationName" in op:
                return str(op["operationName"])
            return getattr(op, "name", str(type(op)))

        if isinstance(ops, list):
            self._last_gql_op = ", ".join(_get_op_name(op) for op in ops)
        else:
            self._last_gql_op = _get_op_name(ops)
        gql_logger.debug(f"GQL Request: {ops}")
        backoff = ExponentialBackoff(maximum=60)
        # Use a flag to retry the request a single time, if a specific set of errors is encountered
        single_retry: bool = True
        for delay in backoff:
            async with self._gql_limiter:
                auth_state = await self.get_auth()
                async with self.request(
                    "POST",
                    "https://gql.twitch.tv/gql",
                    json=ops,
                    headers=auth_state.headers(user_agent=self._client_type.USER_AGENT, gql=True, authenticated=authenticated),
                ) as response:
                    try:
                        response_json: JsonType | list[JsonType] = await response.json()
                    except (aiohttp.ContentTypeError, json.JSONDecodeError):
                        raw_text = await response.text()
                        logger.warning(
                            f"GQL returned non-JSON response ({response.status}): {raw_text[:200]}"
                        )
                        await asyncio.sleep(delay)
                        continue
            gql_logger.debug(f"GQL Response: {response_json}")
            orig_response = response_json
            if isinstance(response_json, list):
                response_list = response_json
            else:
                response_list = [response_json]
            force_retry: bool = False
            for response_json in response_list:
                # GQL error handling
                if "errors" in response_json:
                    for error_dict in response_json["errors"]:
                        if "message" in error_dict:
                            if (
                                single_retry
                                and error_dict["message"] == "service error"
                            ):
                                op_name = (response_json.get("extensions") or {}).get("operationName", "unknown")
                                logger.error(
                                    f"Retrying a {error_dict['message']} for {op_name}"
                                )
                                single_retry = False
                                if delay < 5:
                                    # overwrite the delay if too short
                                    delay = 5
                                force_retry = True
                                break
                            elif error_dict.get("message") == "server error":
                                # nullify the key the error path points to
                                data_dict = response_json.get("data")
                                path = error_dict.get("path")
                                if data_dict is not None and isinstance(data_dict, dict) and path:
                                    curr = data_dict
                                    for key in path[:-1]:
                                        if isinstance(curr, dict) and key in curr:
                                            curr = curr[key]
                                        else:
                                            break
                                    else:
                                        if isinstance(curr, dict) and path[-1] in curr:
                                            curr[path[-1]] = None
                                break
                            elif (
                                error_dict.get("message") in (
                                    "service timeout",
                                    "request cancelled",
                                    "service unavailable",
                                    "context deadline exceeded",
                                )
                            ):
                                force_retry = True
                                break
                    else:
                        raise GQLException(response_json['errors'])
                # Other error handling
                elif "error" in response_json:
                    if response_json.get("status") == 401 or "Unauthorized" in str(response_json.get("error")):
                        logger.warning("OAuth token expired during GQL request. Re-authenticating...")
                        self._auth_state.invalidate(delete_cookies=True)
                        await self.get_auth()
                        force_retry = True
                    else:
                        raise GQLException(
                            f"{response_json['error']}: {response_json.get('message', '')}"
                        )
                if force_retry:
                    break
            else:
                return orig_response
            await asyncio.sleep(delay)
        raise RuntimeError("Retry loop was broken")

    def _merge_data(self, primary_data: JsonType, secondary_data: JsonType) -> JsonType:
        if not isinstance(primary_data, dict) or not isinstance(secondary_data, dict):
            return primary_data if primary_data is not None else secondary_data

        merged: JsonType = {}
        for key in set(chain(primary_data.keys(), secondary_data.keys())):
            in_primary = key in primary_data
            in_secondary = key in secondary_data
            if in_primary and in_secondary:
                vp = primary_data[key]
                vs = secondary_data[key]
                if vp is None:
                    merged[key] = vs
                elif vs is None:
                    merged[key] = vp
                elif isinstance(vp, dict) and isinstance(vs, dict):
                    merged[key] = self._merge_data(vp, vs)
                elif isinstance(vp, list) and isinstance(vs, list):
                    if key == "timeBasedDrops":
                        # Merge drops by drop id to preserve user progress in 'self'
                        p_drops_by_id = {d["id"]: d for d in vp if isinstance(d, dict) and "id" in d}
                        s_drops_by_id = {d["id"]: d for d in vs if isinstance(d, dict) and "id" in d}
                        all_drop_ids = list(dict.fromkeys(list(p_drops_by_id.keys()) + list(s_drops_by_id.keys())))
                        merged_drops = []
                        for did in all_drop_ids:
                            if did in p_drops_by_id and did in s_drops_by_id:
                                merged_drops.append(self._merge_data(p_drops_by_id[did], s_drops_by_id[did]))
                            elif did in p_drops_by_id:
                                merged_drops.append(p_drops_by_id[did])
                            else:
                                merged_drops.append(s_drops_by_id[did])
                        merged[key] = merged_drops
                    else:
                        merged[key] = vp if len(vp) > 0 else vs
                else:
                    merged[key] = vp if vp is not None else vs
            elif in_primary:
                merged[key] = primary_data[key]
            else:
                merged[key] = secondary_data[key]
        return merged

    async def fetch_campaigns(
        self, campaigns_chunk: list[tuple[str, JsonType]]
    ) -> dict[str, JsonType]:
        campaign_ids: dict[str, JsonType] = dict(campaigns_chunk)
        auth_state = await self.get_auth()
        response_list: list[JsonType] = await self.gql_request(
            [
                GQL_QUERIES["CampaignDetails"].with_variables(
                    {"channelLogin": str(auth_state.user_id), "dropID": cid}
                )
                for cid in campaign_ids
            ]
        )
        fetched_data: dict[str, JsonType] = {}
        for response_json in response_list:
            if not isinstance(response_json, dict):
                continue
            user_data = response_json.get("data", {}).get("user")
            if user_data and isinstance(user_data, dict):
                campaign_data = user_data.get("dropCampaign")
                if campaign_data and isinstance(campaign_data, dict) and "id" in campaign_data:
                    fetched_data[campaign_data["id"]] = campaign_data
        return self._merge_data(campaign_ids, fetched_data)

    def _normalize_discovered_campaign(self, c: JsonType, fallback_game: JsonType | None = None) -> JsonType | None:
        summary = c.get("summary") or {}
        if summary.get("isRewardCampaign") or summary.get("includesSubRequirement"):
            return None
        now = datetime.now(timezone.utc)
        game = c.get("game")
        if not game:
            return None
        if isinstance(game, dict):
            game = dict(game)
            if fallback_game and isinstance(fallback_game, dict):
                if not game.get("boxArtURL") and fallback_game.get("boxArtURL"):
                    game["boxArtURL"] = fallback_game["boxArtURL"]
                if not game.get("slug") and fallback_game.get("slug"):
                    game["slug"] = fallback_game["slug"]
                if (not game.get("id") or str(game.get("id")) == "0") and fallback_game.get("id"):
                    game["id"] = fallback_game["id"]
        if not isinstance(game, dict) or not game.get("name"):
            return None
        gid = str(game.get("id", ""))
        if "boxArtURL" not in game or not game["boxArtURL"]:
            if gid and gid != "0":
                game["boxArtURL"] = f"https://static-cdn.jtvnw.net/ttv-boxart/{gid}.jpg"
            else:
                game["boxArtURL"] = "https://static-cdn.jtvnw.net/ttv-static/404_boxart.jpg"
        if not gid:
            game["id"] = "0"

        start_at = (
            c.get("startAt")
            or (c.get("timeBasedDrops", [{}])[0].get("startAt") if c.get("timeBasedDrops") else None)
            or now.isoformat()
        )
        end_at = (
            c.get("endAt")
            or (c.get("timeBasedDrops", [{}])[0].get("endAt") if c.get("timeBasedDrops") else None)
            or (now + timedelta(days=7)).isoformat()
        )
        self_data = c.get("self") or {"isAccountConnected": True}
        allow_data = c.get("allow") or {"channels": None, "isEnabled": False}

        time_based = []
        for d in c.get("timeBasedDrops", []) or []:
            if not isinstance(d, dict):
                continue
            drop = dict(d)
            if "self" not in drop or not drop["self"]:
                drop["self"] = None
            if "preconditionDrops" not in drop:
                drop["preconditionDrops"] = []
            if "startAt" not in drop:
                drop["startAt"] = start_at
            if "endAt" not in drop:
                drop["endAt"] = end_at
            benefit_edges = []
            for b_edge in drop.get("benefitEdges", []) or []:
                if isinstance(b_edge, dict):
                    b_edge_copy = dict(b_edge)
                    b_data = dict(b_edge.get("benefit", {})) if isinstance(b_edge.get("benefit"), dict) else {}
                    if "distributionType" not in b_data:
                        b_data["distributionType"] = "UNKNOWN"
                    if "imageAssetURL" not in b_data:
                        b_data["imageAssetURL"] = b_data.get("imageURL") or ""
                    b_edge_copy["benefit"] = b_data
                    benefit_edges.append(b_edge_copy)
            drop["benefitEdges"] = benefit_edges
            time_based.append(drop)

        return {
            "id": c.get("id", ""),
            "name": c.get("name", ""),
            "game": game,
            "imageURL": c.get("imageURL") or "",
            "self": self_data,
            "accountLinkURL": c.get("accountLinkURL") or c.get("detailsURL") or "",
            "startAt": start_at,
            "endAt": end_at,
            "status": c.get("status", "ACTIVE"),
            "allow": allow_data,
            "timeBasedDrops": time_based,
            "eventBasedDrops": c.get("eventBasedDrops") or [],
        }

    async def _resolve_game_slug(self, game_name: str) -> str:
        if game_name in self._resolved_game_slugs:
            return self._resolved_game_slugs[game_name]
        naive_slug = re.sub(r'-{2,}', '-', re.sub(r'\W+', '-', re.sub(r'\'', '', game_name.lower())).strip('-'))
        try:
            sq = {
                "query": 'query SearchGame($query: String!) { searchFor(userQuery: $query, platform: "web") { games { items { id name slug } } } }',
                "variables": {"query": game_name},
            }
            res = await self.gql_request(sq, authenticated=False)
            if isinstance(res, dict):
                items = res.get("data", {}).get("searchFor", {}).get("games", {}).get("items", [])
                if items and isinstance(items, list):
                    best = items[0]
                    if best and best.get("slug"):
                        slug = str(best["slug"])
                        self._resolved_game_slugs[game_name] = slug
                        return slug
        except Exception as e:
            logger.debug(f"Search for game '{game_name}' slug failed: {e}")
        self._resolved_game_slugs[game_name] = naive_slug
        return naive_slug

    async def _discover_campaigns(self) -> dict[str, JsonType]:
        popular_slugs = [
            "warframe", "rust", "albion-online", "overwatch-2", "valorant", "world-of-warcraft",
            "dead-by-daylight", "pubg-battlegrounds", "apex-legends", "rainbow-six-siege",
            "escape-from-tarkov", "sea-of-thieves", "the-elder-scrolls-online", "smite-2",
            "path-of-exile", "path-of-exile-2", "destiny-2", "counter-strike", "league-of-legends",
            "teamfight-tactics", "hearthstone", "fortnite", "genshin-impact", "honkai-star-rail",
            "zenless-zone-zero", "the-first-descendant", "throne-and-liberty", "marvel-rivals",
            "once-human", "lost-ark", "diablo-iv", "black-desert", "fallout-76", "guild-wars-2",
            "hunt-showdown", "paladins", "deadlock", "star-citizen", "cyberpunk-2077", "no-mans-sky",
            "brawlhalla", "world-of-tanks", "delta-force", "dungeon-fighter-online", "conan-exiles",
            "arena-breakout-infinite", "dying-light-2", "ark-survival-ascended", "space-marine-2",
            "street-fighter-6", "tekken-8", "payday-3", "the-division-2", "magic-the-gathering-arena",
            "yu-gi-oh-master-duel", "naraka-bladepoint", "eternal-return", "dark-and-darker",
            "wuthering-waves", "world-of-warships", "war-thunder", "enlisted", "crossout",
            "star-trek-online", "neverwinter", "star-wars-the-old-republic", "old-school-runescape",
            "runescape", "dayz", "scum", "7-days-to-die", "project-zomboid", "sons-of-the-forest",
            "valheim", "satisfactory", "deep-rock-galactic", "helldivers-2", "darktide",
            "killing-floor-2", "phasmophobia", "lethal-company", "the-finals", "xdefiant",
            "arc-raiders", "alien-isolation", "dragons-dogma-ii", "elden-ring", "the-witcher-3-wild-hunt",
        ]
        priority_slugs: list[str] = []
        for g in self.settings.priority:
            if g:
                naive = re.sub(r'-{2,}', '-', re.sub(r'\W+', '-', re.sub(r'\'', '', g.lower())).strip('-'))
                resolved = await self._resolve_game_slug(g)
                for s in (resolved, naive):
                    if s and s not in priority_slugs:
                        priority_slugs.append(s)

        exclude_slugs: set[str] = set()
        for g in self.settings.exclude:
            if g:
                naive = re.sub(r'-{2,}', '-', re.sub(r'\W+', '-', re.sub(r'\'', '', g.lower())).strip('-'))
                resolved = await self._resolve_game_slug(g)
                if resolved:
                    exclude_slugs.add(resolved)
                if naive:
                    exclude_slugs.add(naive)

        # Dynamic discovery: query Twitch Browse to discover any active games/categories currently streaming
        dynamic_slugs: list[str] = []
        non_game_slugs = {
            "just-chatting", "special-events", "talk-shows-podcasts", "retro", "art",
            "music", "asmr", "pools-bikinis-and-beaches", "sports", "always-on", "games-demos",
        }
        try:
            browse_resp = await self.gql_request(GQL_QUERIES["BrowseAllDirectories"], authenticated=False)
            edges = browse_resp.get("data", {}).get("directoriesWithTags", {}).get("edges", [])
            for edge in edges:
                slug = edge.get("node", {}).get("slug")
                if slug and slug not in non_game_slugs:
                    dynamic_slugs.append(slug)
        except Exception as e:
            logger.debug(f"Dynamic directory query failed: {e}")

        all_slugs = [
            s for s in dict.fromkeys(priority_slugs + dynamic_slugs + popular_slugs)
            if s not in exclude_slugs
        ]

        game_queries = [
            GQL_QUERIES["GameDirectory"].with_variables({
                "slug": slug,
                "limit": 8 if slug in priority_slugs else 6,
                "options": {
                    "includeRestricted": ["SUB_ONLY_LIVE"],
                    "systemFilters": ["DROPS_ENABLED"],
                },
            })
            for slug in all_slugs
        ]
        streamer_ids: list[str] = []
        streamer_game_map: dict[str, JsonType] = {}
        games_by_id: dict[str, JsonType] = {}
        games_by_name: dict[str, JsonType] = {}
        priority_empty_slugs: list[str] = []

        for slug_chunk in chunk(game_queries, 20):
            try:
                res_list: list[JsonType] = await self.gql_request(slug_chunk, authenticated=False)
                for res in res_list:
                    game_data = res.get("data", {}).get("game")
                    if game_data and game_data.get("name"):
                        g_info = {
                            "id": str(game_data.get("id", "")),
                            "name": game_data.get("name"),
                            "slug": game_data.get("slug"),
                            "boxArtURL": game_data.get("boxArtURL"),
                        }
                        if g_info["id"]:
                            games_by_id[g_info["id"]] = g_info
                        games_by_name[g_info["name"].lower()] = g_info

                        streams = game_data.get("streams", {}).get("edges", [])
                        if not streams and g_info.get("slug") in priority_slugs:
                            priority_empty_slugs.append(g_info["slug"])

                        stream_limit = 4 if g_info.get("slug") in priority_slugs else 2
                        for st in streams[:stream_limit]:
                            bid = st.get("node", {}).get("broadcaster", {}).get("id")
                            if bid:
                                sid_str = str(bid)
                                streamer_ids.append(sid_str)
                                streamer_game_map[sid_str] = g_info
            except Exception as e:
                logger.debug(f"Game directory query chunk error: {e}")

        # Fallback for priority games that returned 0 streams with DROPS_ENABLED filter:
        # check top live channels without filter in case broadcasters did not tag their stream
        if priority_empty_slugs:
            fallback_queries = [
                GQL_QUERIES["GameDirectory"].with_variables({"slug": slug, "limit": 6})
                for slug in priority_empty_slugs
            ]
            for p_chunk in chunk(fallback_queries, 20):
                try:
                    res_list = await self.gql_request(p_chunk, authenticated=False)
                    for res in res_list:
                        game_data = res.get("data", {}).get("game")
                        if game_data and game_data.get("name"):
                            g_info = {
                                "id": str(game_data.get("id", "")),
                                "name": game_data.get("name"),
                                "slug": game_data.get("slug"),
                                "boxArtURL": game_data.get("boxArtURL"),
                            }
                            streams = game_data.get("streams", {}).get("edges", [])
                            for st in streams[:4]:
                                bid = st.get("node", {}).get("broadcaster", {}).get("id")
                                if bid:
                                    sid_str = str(bid)
                                    streamer_ids.append(sid_str)
                                    streamer_game_map[sid_str] = g_info
                except Exception as e:
                    logger.debug(f"Priority fallback query chunk error: {e}")

        discovered: dict[str, JsonType] = {}
        if streamer_ids:
            drop_queries = [
                GQL_QUERIES["AvailableDrops"].with_variables({"channelID": sid})
                for sid in list(dict.fromkeys(streamer_ids))
            ]
            for drop_chunk in chunk(drop_queries, 25):
                try:
                    drop_res_list: list[JsonType] = await self.gql_request(drop_chunk, authenticated=False)
                    for dres in drop_res_list:
                        if not isinstance(dres, dict):
                            continue
                        channel_data = dres.get("data", {}).get("channel") or {}
                        camps = channel_data.get("viewerDropCampaigns") or []
                        for c in camps:
                            if c and "id" in c and c["id"]:
                                cid = c["id"]
                                if cid not in discovered:
                                    summary = c.get("summary") or {}
                                    if summary.get("isRewardCampaign") or summary.get("includesSubRequirement"):
                                        continue
                                    c_game = c.get("game") or {}
                                    if not c_game or not c_game.get("name"):
                                        continue
                                    c_gid = str(c_game.get("id", ""))
                                    c_name = str(c_game.get("name", "")).lower()
                                    fallback_game = (
                                        games_by_id.get(c_gid)
                                        or games_by_name.get(c_name)
                                    )
                                    norm = self._normalize_discovered_campaign(c, fallback_game=fallback_game)
                                    if norm and norm.get("game"):
                                        discovered[cid] = norm
                except Exception as e:
                    logger.debug(f"Available drops query chunk error: {e}")

        # Preserve active discovered campaigns until their end date
        now = datetime.now(timezone.utc)
        for cid in list(self._known_discovered_campaigns.keys()):
            c_data = self._known_discovered_campaigns[cid]
            end_at = c_data.get("endAt")
            if end_at:
                try:
                    dt = timestamp(end_at) if isinstance(end_at, str) else end_at
                    if dt <= now:
                        del self._known_discovered_campaigns[cid]
                except Exception:
                    pass

        self._known_discovered_campaigns.update(discovered)
        return dict(self._known_discovered_campaigns)

    async def fetch_inventory(self) -> None:
        status_update = self.gui.status.update
        status_update(_("gui", "status", "fetching_inventory"))
        # fetch in-progress campaigns (inventory)
        response = await self.gql_request(GQL_QUERIES["Inventory"])
        data = response.get("data") or {}
        user_data = data.get("currentUser")
        if not user_data or not isinstance(user_data, dict):
            raise GQLException("Failed to fetch inventory: currentUser is missing")
        inventory: JsonType = user_data.get("inventory") or {}
        ongoing_campaigns: list[JsonType] = inventory.get("dropCampaignsInProgress") or []
        # this contains claimed benefit edge IDs, not drop IDs
        claimed_benefits: dict[str, list[datetime]] = defaultdict(list)
        for b in inventory.get("gameEventDrops") or []:
            if isinstance(b, dict) and "id" in b and "lastAwardedAt" in b and b["lastAwardedAt"]:
                claimed_benefits[b["id"]].append(timestamp(b["lastAwardedAt"]))
        inventory_data: dict[str, JsonType] = {c["id"]: c for c in ongoing_campaigns}
        # fetch general available campaigns data (campaigns)
        try:
            response = await self.gql_request(GQL_QUERIES["Campaigns"])
            available_list: list[JsonType] = response.get("data", {}).get("currentUser", {}).get("dropCampaigns") or []
        except Exception:
            available_list = []
        applicable_statuses = ("ACTIVE", "UPCOMING")
        available_campaigns: dict[str, JsonType] = {
            c["id"]: c
            for c in available_list
            if c and c.get("status") in applicable_statuses  # that are currently not expired
        }
        # fetch detailed data for each campaign, in chunks
        status_update(_("gui", "status", "fetching_campaigns"))
        if available_campaigns:
            fetch_campaigns_tasks: list[asyncio.Task[Any]] = [
                asyncio.create_task(self.fetch_campaigns(campaigns_chunk))
                for campaigns_chunk in chunk(available_campaigns.items(), 20)
            ]
            try:
                for coro in asyncio.as_completed(fetch_campaigns_tasks):
                    try:
                        chunk_campaigns_data = await coro
                        # merge the inventory and campaigns datas together
                        inventory_data = self._merge_data(inventory_data, chunk_campaigns_data)
                    except Exception as chunk_exc:
                        logger.warning(f"Error fetching campaign details chunk: {chunk_exc}")
            except Exception:
                # asyncio.as_completed doesn't cancel tasks on errors
                for task in fetch_campaigns_tasks:
                    task.cancel()
                raise
        else:
            # Fallback discovery for all games when ViewerDropsDashboard is blocked by integrity/empty
            discovered_campaigns = await self._discover_campaigns()
            inventory_data = self._merge_data(inventory_data, discovered_campaigns)
        # filter out invalid campaigns
        for campaign_id in list(inventory_data.keys()):
            if inventory_data[campaign_id]["game"] is None:
                del inventory_data[campaign_id]

        if self.settings.dump:
            # dump the campaigns data to the dump file
            with open(DUMP_PATH, 'a', encoding="utf8") as file:
                # we need to pre-process the inventory dump a little
                dump_data: JsonType = deepcopy(inventory_data)
                for campaign_data in dump_data.values():
                    # replace ACL lists with a simple text description
                    if (
                        campaign_data["allow"]
                        and campaign_data["allow"].get("isEnabled", True)
                        and campaign_data["allow"]["channels"]
                    ):
                        # simply count the channels included in the ACL
                        campaign_data["allow"]["channels"] = (
                            f"{len(campaign_data['allow']['channels'])} channels"
                        )
                    # replace drop instance IDs, so they don't include user IDs
                    for drop_data in campaign_data["timeBasedDrops"]:
                        if "self" in drop_data and drop_data["self"]["dropInstanceID"]:
                            drop_data["self"]["dropInstanceID"] = "..."
                json.dump(dump_data, file, indent=4, sort_keys=True)
                file.write("\n\n")  # add 2x new line spacer
                json.dump(inventory["gameEventDrops"], file, indent=4, sort_keys=True, default=str)

        # use the merged data to create campaign objects
        campaigns: list[DropsCampaign] = [
            DropsCampaign(self, campaign_data, claimed_benefits)
            for campaign_data in inventory_data.values()
        ]
        campaigns.sort(key=lambda c: c.active, reverse=True)
        campaigns.sort(key=lambda c: c.upcoming and c.starts_at or c.ends_at)
        campaigns.sort(key=lambda c: c.eligible, reverse=True)

        self._drops.clear()
        self._campaigns.clear()
        self.gui.inv.clear()
        self.inventory.clear()
        self._mnt_triggers.clear()
        switch_triggers: set[datetime] = set()
        next_hour = datetime.now(timezone.utc) + timedelta(hours=1)
        # add the campaigns to the internal inventory
        for campaign in campaigns:
            self._drops.update({drop.id: drop for drop in campaign.drops})
            if campaign.can_earn_within(next_hour):
                switch_triggers.update(campaign.time_triggers)
            self.inventory.append(campaign)
            self._campaigns[campaign.id] = campaign
        logger.info(f"Inventory loaded: {len(campaigns)} active/upcoming drop campaigns found.")
        # concurrently add the campaigns into the GUI
        # NOTE: this fetches pictures from the CDN, so might be slow without a cache
        status_update(
            _("gui", "status", "adding_campaigns").format(counter=f"(0/{len(campaigns)})")
        )
        add_campaign_tasks: list[asyncio.Task[None]] = [
            asyncio.create_task(self.gui.inv.add_campaign(campaign))
            for campaign in campaigns
        ]
        try:
            for i, coro in enumerate(asyncio.as_completed(add_campaign_tasks), start=1):
                try:
                    await coro
                except Exception as exc:
                    logger.debug(f"Error adding campaign to GUI: {exc}")
                if i % 5 == 0 or i == len(campaigns):
                    status_update(
                        _("gui", "status", "adding_campaigns").format(
                            counter=f"({i}/{len(campaigns)})"
                        )
                    )
                # this is needed here explicitly, because cache reads from disk don't raise this
                if self.gui.close_requested:
                    raise ExitRequest()
        except ExitRequest:
            raise
        except Exception:
            # asyncio.as_completed doesn't cancel tasks on errors
            for task in add_campaign_tasks:
                task.cancel()
            raise
        self._mnt_triggers.extend(sorted(switch_triggers))
        # trim out all triggers that we're already past
        now = datetime.now(timezone.utc)
        while self._mnt_triggers and self._mnt_triggers[0] <= now:
            self._mnt_triggers.popleft()
        # NOTE: maintenance task is restarted at the end of each inventory fetch
        if self._mnt_task is not None and not self._mnt_task.done():
            self._mnt_task.cancel()
        self._mnt_task = asyncio.create_task(self._maintenance_task())

    def get_active_campaign(self, channel: Channel | None = None) -> DropsCampaign | None:
        if not self.wanted_games:
            return None
        watching_channel = self.watching_channel.get_with_default(channel)
        if watching_channel is None:
            # if we aren't watching anything, we can't earn any drops
            return None
        campaigns: list[DropsCampaign] = [
            c for c in self.inventory if c.can_earn(watching_channel)
        ]
        if not campaigns:
            return None
        priority_mode = self.settings.priority_mode
        if priority_mode is PriorityMode.ENDING_SOONEST:
            campaigns.sort(key=lambda c: c.ends_at)
        elif priority_mode is PriorityMode.LOW_AVBL_FIRST:
            campaigns.sort(key=lambda c: c.availability)
        elif priority_mode is PriorityMode.SHORTEST_TIME_FIRST:
            campaigns.sort(
                key=lambda c: (
                    c.first_drop.remaining_minutes
                    if c.first_drop
                    else c.remaining_minutes
                )
            )
        else:
            campaigns.sort(key=lambda c: c.remaining_minutes)
        # Always prioritize:
        # 1. Exact game match (channel is currently streaming this campaign's game)
        # 2. User priority order from self.wanted_games
        campaigns.sort(
            key=lambda c: (
                0 if (watching_channel.game and c.game == watching_channel.game) else 1,
                self.wanted_games.index(c.game) if c.game in self.wanted_games else MAX_INT,
            )
        )
        return campaigns[0]

    async def get_live_streams(
        self, game: Game, *, limit: int = 20, drops_enabled: bool = True
    ) -> list[Channel]:
        if not game or not game.slug:
            return []
        filters: list[str] = []
        if drops_enabled:
            filters.append("DROPS_ENABLED")
        try:
            response = await self.gql_request(
                GQL_QUERIES["GameDirectory"].with_variables({
                    "limit": limit,
                    "slug": game.slug,
                    "options": {
                        "includeRestricted": ["SUB_ONLY_LIVE"],
                        "systemFilters": filters,
                    },
                })
            )
        except GQLException as exc:
            logger.warning(f"Error fetching live streams for game {game}: {exc}")
            return []
        game_data = (response.get("data") or {}).get("game")
        if game_data and game_data.get("streams") and game_data["streams"].get("edges"):
            return [
                Channel.from_directory(
                    self, stream_channel_data["node"], drops_enabled=drops_enabled
                )
                for stream_channel_data in game_data["streams"]["edges"]
                if stream_channel_data.get("node") and stream_channel_data["node"].get("broadcaster") is not None
            ]
        return []

    async def bulk_check_online(self, channels: abc.Iterable[Channel]):
        """
        Utilize batch GQL requests to check ONLINE status for a lot of channels at once.
        Also handles the drops_enabled check (if enabled).
        """
        acl_streams_map: dict[int, JsonType] = {}
        stream_gql_ops: list[GQLOperation] = [channel.stream_gql for channel in channels]
        if not stream_gql_ops:
            # shortcut for nothing to process
            # NOTE: Have to do this here, becase "channels" can be any iterable
            return
        stream_gql_tasks: list[asyncio.Task[list[JsonType]]] = [
            asyncio.create_task(self.gql_request(stream_gql_chunk))
            for stream_gql_chunk in chunk(stream_gql_ops, 20)
        ]
        try:
            for coro in asyncio.as_completed(stream_gql_tasks):
                response_list: list[JsonType] = await coro
                for response_json in response_list:
                    data = response_json.get("data") if isinstance(response_json, dict) else None
                    if data and isinstance(data, dict):
                        channel_data: JsonType | None = data.get("user")
                        if channel_data is not None and isinstance(channel_data, dict) and "id" in channel_data:
                            acl_streams_map[int(channel_data["id"])] = channel_data
        except Exception:
            # asyncio.as_completed doesn't cancel tasks on errors
            for task in stream_gql_tasks:
                task.cancel()
            raise
        # for all channels with an active stream, check the available drops as well
        acl_available_drops_map: dict[int, list[JsonType]] = {}
        if self.settings.available_drops_check:
            available_gql_ops: list[GQLOperation] = [
                GQL_QUERIES["AvailableDrops"].with_variables({"channelID": str(channel_id)})
                for channel_id, channel_data in acl_streams_map.items()
                if channel_data.get("stream") is not None  # only do this for ONLINE channels
            ]
            available_gql_tasks: list[asyncio.Task[list[JsonType]]] = [
                asyncio.create_task(self.gql_request(available_gql_chunk))
                for available_gql_chunk in chunk(available_gql_ops, 20)
            ]
            try:
                for coro in asyncio.as_completed(available_gql_tasks):
                    response_list = await coro
                    for response_json in response_list:
                        data = response_json.get("data") if isinstance(response_json, dict) else None
                        if data and isinstance(data, dict):
                            available_info: JsonType | None = data.get("channel")
                            if available_info is not None and isinstance(available_info, dict) and "id" in available_info:
                                acl_available_drops_map[int(available_info["id"])] = (
                                    available_info.get("viewerDropCampaigns") or []
                                )
            except Exception:
                # asyncio.as_completed doesn't cancel tasks on errors
                for task in available_gql_tasks:
                    task.cancel()
                raise
        for channel in channels:
            channel_id = channel.id
            if channel_id not in acl_streams_map:
                continue
            channel_data = acl_streams_map[channel_id]
            if channel_data["stream"] is None:
                continue
            available_drops: list[JsonType] = acl_available_drops_map.get(channel_id, [])
            channel.external_update(channel_data, available_drops)

    async def bulk_check_available_drops(self, channels: abc.Iterable[Channel]) -> None:
        """
        Check available drops for already-online channels (e.g. from game directory).
        Updates stream.drops_enabled based on whether earnable drops exist.
        """
        if not self.settings.available_drops_check:
            return
        online_channels = [ch for ch in channels if ch.online and ch._stream is not None]
        if not online_channels:
            return
        available_gql_ops: list[GQLOperation] = [
            GQL_QUERIES["AvailableDrops"].with_variables({"channelID": str(ch.id)})
            for ch in online_channels
        ]
        available_drops_map: dict[int, list[JsonType]] = {}
        available_gql_tasks: list[asyncio.Task[list[JsonType]]] = [
            asyncio.create_task(self.gql_request(available_gql_chunk))
            for available_gql_chunk in chunk(available_gql_ops, 20)
        ]
        try:
            for coro in asyncio.as_completed(available_gql_tasks):
                response_list = await coro
                if isinstance(response_list, dict):
                    response_list = [response_list]
                for response_json in response_list:
                    data = response_json.get("data") if isinstance(response_json, dict) else None
                    if data and isinstance(data, dict):
                        available_info: JsonType | None = data.get("channel")
                        if available_info is not None and isinstance(available_info, dict) and "id" in available_info:
                            available_drops_map[int(available_info["id"])] = (
                                available_info.get("viewerDropCampaigns") or []
                            )
        except Exception as exc:
            for task in available_gql_tasks:
                task.cancel()
            logger.debug(f"Error checking available drops for directory channels: {exc}")
            return
        for ch in online_channels:
            if ch._stream is not None and ch.id in available_drops_map:
                camps = available_drops_map[ch.id]
                ch._stream.drops_enabled = ch._check_drops_enabled(camps)
