"""Проверки коннектора Telegram.

Настоящий вход в Telegram здесь недоступен, поэтому проверяется вся логика
превращения данных Telegram в наблюдения — на поддельном клиенте, который
повторяет поведение настоящего.
"""

import asyncio
import unittest
from datetime import datetime

from connectors.telegram import (
    TelegramCheckSettings,
    collect,
    is_subscription,
    normalize_chat_id,
    observations_from_dialog,
    observations_from_message,
)
from matching.engine import EXACT, MatchEngine
from tests.fake_telegram import (
    Channel,
    Chat,
    Dialog,
    FakeClient,
    ForwardHeader,
    Message,
    PeerChannel,
    UrlEntity,
    User,
    Username,
)
from tests.test_matching import build_index


def drain(client, settings):
    async def run():
        return [o async for o in collect(client, settings)]

    return asyncio.run(run())


def settings(**kwargs):
    base = dict(api_id=1, api_hash="x", message_limit=100, pause=0)
    base.update(kwargs)
    return TelegramCheckSettings(**base)


class TestChatIds(unittest.TestCase):
    def test_plain_id_is_kept(self):
        self.assertEqual(normalize_chat_id(1442177936), "1442177936")

    def test_prefixed_id_is_reduced_to_the_registry_form(self):
        """Telegram показывает тот же канал и как -1001442177936."""
        self.assertEqual(normalize_chat_id(-1001442177936), "1442177936")

    def test_small_ids_are_not_damaged(self):
        self.assertEqual(normalize_chat_id(100200), "100200")


class TestSubscriptions(unittest.TestCase):
    def test_channel_gives_both_name_and_id(self):
        """Имя канал может сменить, идентификатор — нет."""
        entity = Channel(1442177936, "Мозырь 97%", username="mozyr97pro")
        keys = {o.key for o in observations_from_dialog(entity)}
        self.assertEqual(
            keys,
            {"telegram:handle:mozyr97pro", "telegram:numeric_id:1442177936"},
        )

    def test_all_extra_usernames_are_taken(self):
        entity = Channel(
            5, "Канал", username="main", usernames=[Username("spare"), Username("third")]
        )
        handles = {o.value for o in observations_from_dialog(entity) if o.kind == "handle"}
        self.assertEqual(handles, {"main", "spare", "third"})

    def test_group_without_a_name_still_gives_its_id(self):
        """Ровно такие записи в реестре опознаются только по идентификатору."""
        keys = {o.key for o in observations_from_dialog(Chat(1223603803, "Чат"))}
        self.assertEqual(keys, {"telegram:numeric_id:1223603803"})

    def test_private_chat_is_not_a_subscription(self):
        self.assertFalse(is_subscription(User(7, username="ivan")))
        self.assertEqual(observations_from_dialog(User(7, username="ivan")), [])

    def test_trace_type_and_source_are_filled(self):
        observation = observations_from_dialog(Channel(1, "Название", username="x"))[0]
        self.assertEqual(observation.trace_type, "subscription")
        self.assertIn("Название", observation.source)


class TestMessages(unittest.TestCase):
    def test_forward_is_recorded_with_its_origin(self):
        message = Message(
            message="",
            fwd_from=ForwardHeader(PeerChannel(1442177936), "Мозырь 97%"),
            date=datetime(2026, 3, 12),
        )
        observations = observations_from_message(message, "Избранное")
        self.assertEqual(observations[0].key, "telegram:numeric_id:1442177936")
        self.assertEqual(observations[0].trace_type, "repost")
        self.assertEqual(observations[0].occurred_at, "2026-03-12")

    def test_link_in_text_is_found(self):
        message = Message(message="смотри https://t.me/badchannel вот")
        keys = {o.key for o in observations_from_message(message, "Чат")}
        self.assertIn("telegram:handle:badchannel", keys)

    def test_link_hidden_behind_text_is_found(self):
        """В тексте виден только «тут», настоящий адрес лежит отдельно."""
        message = Message(message="тут", entities=[UrlEntity("https://t.me/badchannel")])
        keys = {o.key for o in observations_from_message(message, "Чат")}
        self.assertIn("telegram:handle:badchannel", keys)

    def test_link_to_another_network_is_also_found(self):
        message = Message(message="https://vk.com/badgroup")
        keys = {o.key for o in observations_from_message(message, "Чат")}
        self.assertIn("vk:handle:badgroup", keys)

    def test_ordinary_message_gives_nothing(self):
        self.assertEqual(observations_from_message(Message(message="привет"), "Чат"), [])


class TestCollecting(unittest.TestCase):
    def setUp(self):
        self.channel = Channel(1442177936, "Плохой канал", username="badchannel")
        self.person = User(7, username="ivan")
        self.dialogs = [Dialog(self.channel, "Плохой канал"), Dialog(self.person, "Иван")]
        self.messages = {
            self.channel.id: [Message(message="https://t.me/other")],
            self.person.id: [
                Message(
                    message="",
                    fwd_from=ForwardHeader(PeerChannel(1223603803)),
                    date=datetime(2026, 1, 5),
                )
            ],
        }

    def test_subscriptions_and_messages_are_both_collected(self):
        client = FakeClient(self.dialogs, self.messages)
        keys = {o.key for o in drain(client, settings())}
        self.assertIn("telegram:handle:badchannel", keys)
        self.assertIn("telegram:handle:other", keys)
        self.assertIn("telegram:numeric_id:1223603803", keys)

    def test_subscriptions_only_mode_reads_no_messages(self):
        client = FakeClient(self.dialogs, self.messages)
        keys = {o.key for o in drain(client, settings(read_messages=False))}
        self.assertIn("telegram:handle:badchannel", keys)
        self.assertNotIn("telegram:handle:other", keys)
        self.assertEqual(client.requests, [], "сообщения не должны запрашиваться")

    def test_own_messages_only_by_default(self):
        client = FakeClient(self.dialogs, self.messages)
        drain(client, settings())
        senders = {request[3] for request in client.requests}
        self.assertEqual(senders, {"me"})

    def test_all_messages_mode_widens_the_search(self):
        client = FakeClient(self.dialogs, self.messages)
        drain(client, settings(own_messages_only=False))
        senders = {request[3] for request in client.requests}
        self.assertEqual(senders, {None})

    def test_each_dialog_is_read_exactly_once(self):
        """Повторный проход по тому же диалогу удваивал бы нагрузку на аккаунт
        без всякой пользы — именно за такую нагрузку Telegram ограничивает."""
        client = FakeClient(self.dialogs, self.messages)
        drain(client, settings())
        read = [request[0] for request in client.requests]
        self.assertEqual(len(read), len(set(read)))
        self.assertEqual(len(read), len(self.dialogs))

    def test_message_limit_is_passed_through(self):
        client = FakeClient(self.dialogs, self.messages)
        drain(client, settings(message_limit=50))
        self.assertTrue(all(request[1] == 50 for request in client.requests))

    def test_duplicates_are_not_repeated(self):
        client = FakeClient(
            [Dialog(self.channel, "Плохой канал")],
            {self.channel.id: [Message(message="https://t.me/badchannel")] * 5},
        )
        observations = drain(client, settings())
        self.assertEqual(len(observations), len({o.key for o in observations}))

    def test_unreadable_dialog_does_not_stop_the_check(self):
        """Закрытый диалог — обычное дело, проверка должна идти дальше."""
        current = settings()
        client = FakeClient(self.dialogs, self.messages, unreadable={self.channel.id})
        keys = {o.key for o in drain(client, current)}
        self.assertIn("telegram:numeric_id:1223603803", keys)
        self.assertTrue(current.errors)


class TestEndToEnd(unittest.TestCase):
    """Полный путь: Telegram -> сопоставление -> совпадение."""

    def test_subscription_to_a_registry_channel_is_found(self):
        engine = MatchEngine(
            build_index("Telegram-канал https://t.me/badchannel")
        )
        client = FakeClient([Dialog(Channel(999, "Канал", username="badchannel"), "Канал")])
        matches = engine.match_all(drain(client, settings(read_messages=False)))
        self.assertEqual(matches[0].confidence, EXACT)
        self.assertEqual(matches[0].observation.trace_type, "subscription")

    def test_channel_known_only_by_number_is_found(self):
        """В реестре 393 такие записи: имени канала нет, есть только число."""
        engine = MatchEngine(
            build_index(
                "Telegram-чат, имеющий идентификатор (ID), содержащий "
                "последовательность цифр 1223603803"
            )
        )
        client = FakeClient([Dialog(Chat(1223603803, "Чат"), "Чат")])
        matches = engine.match_all(drain(client, settings(read_messages=False)))
        self.assertEqual(matches[0].confidence, EXACT)

    def test_renamed_channel_still_matches_by_its_number(self):
        """Канал сменил имя — по имени потерялся бы, по номеру находится."""
        engine = MatchEngine(
            build_index(
                "Telegram-группа https://t.me/oldname и идентификатор (ID), "
                "содержащий последовательность цифр 1442177936"
            )
        )
        client = FakeClient(
            [Dialog(Channel(1442177936, "Канал", username="brandnewname"), "Канал")]
        )
        matches = engine.match_all(drain(client, settings(read_messages=False)))
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].confidence, EXACT)
        self.assertEqual(matches[0].kind, "numeric_id")

    def test_ordinary_account_produces_no_matches(self):
        engine = MatchEngine(build_index("Telegram-канал https://t.me/badchannel"))
        client = FakeClient(
            [Dialog(Channel(555, "Семья", username="my_family_chat"), "Семья")],
            {555: [Message(message="привет, как дела")]},
        )
        self.assertEqual(engine.match_all(drain(client, settings())), [])


if __name__ == "__main__":
    unittest.main()
