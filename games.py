# =========================================================
# BET BOT - GAMES
# =========================================================

import random


# =========================================================
# مشخصات بازی‌ها
# =========================================================

GAME_INFO = {
    "dice": {
        "name": "🎲 تاس",
        "emoji": "🎲",
        "max_value": 6,
    },

    "bowling": {
        "name": "🎳 بولینگ",
        "emoji": "🎳",
        "max_value": 6,
    },

    "basketball": {
        "name": "🏀 بسکتبال",
        "emoji": "🏀",
        "max_value": 5,
    },

    "darts": {
        "name": "🎯 دارت",
        "emoji": "🎯",
        "max_value": 6,
    },
}


# =========================================================
# بررسی بازی
# =========================================================

def is_valid_game(game: str) -> bool:
    return game in GAME_INFO


def get_game_name(game: str) -> str:
    info = GAME_INFO.get(game)

    if not info:
        return "🎮 بازی"

    return info["name"]


# =========================================================
# مقدار تصادفی بازی
# =========================================================

def roll_game(game: str) -> int:
    """
    نتیجه داخلی بازی را تولید می‌کند.
    """

    info = GAME_INFO.get(game)

    if not info:
        raise ValueError("Unknown game")

    return random.randint(
        1,
        info["max_value"]
    )


# =========================================================
# برنده یک دور
# =========================================================

def compare_results(player1: int, player2: int):
    if player1 > player2:
        return "player1"

    if player2 > player1:
        return "player2"

    return "draw"


# =========================================================
# بازی با ربات
# =========================================================

def play_against_bot(game: str):
    player = roll_game(game)
    bot = roll_game(game)

    result = compare_results(
        player,
        bot
    )

    return {
        "player": player,
        "bot": bot,
        "result": result,
    }


# =========================================================
# بازی دو نفره
# =========================================================

def play_two_players(game: str):
    player1 = roll_game(game)
    player2 = roll_game(game)

    result = compare_results(
        player1,
        player2
    )

    return {
        "player1": player1,
        "player2": player2,
        "result": result,
    }


# =========================================================
# بازی تا مشخص شدن برنده
# =========================================================

def play_until_winner(game: str):
    """
    در صورت مساوی دوباره بازی می‌شود.
    """

    rounds = []

    while True:
        player1 = roll_game(game)
        player2 = roll_game(game)

        result = compare_results(
            player1,
            player2
        )

        rounds.append({
            "player1": player1,
            "player2": player2,
            "result": result,
        })

        if result != "draw":
            break

    return rounds


# =========================================================
# نتیجه قابل نمایش بازی دو نفره
# =========================================================

def format_two_player_result(
    game: str,
    player1_name: str,
    player2_name: str,
    rounds
):
    game_name = get_game_name(game)

    lines = [
        f"{game_name}",
        "",
    ]

    for index, round_data in enumerate(rounds, start=1):

        p1 = round_data["player1"]
        p2 = round_data["player2"]

        lines.append(
            f"دور {index}: "
            f"{player1_name} → {p1} | "
            f"{player2_name} → {p2}"
        )

    final = rounds[-1]["result"]

    if final == "player1":
        lines.append("")
        lines.append(
            f"🏆 برنده: {player1_name}"
        )

    elif final == "player2":
        lines.append("")
        lines.append(
            f"🏆 برنده: {player2_name}"
        )

    return "\n".join(lines)


# =========================================================
# نتیجه قابل نمایش بازی با ربات
# =========================================================

def format_bot_result(
    game: str,
    player_name: str,
    rounds
):
    game_name = get_game_name(game)

    lines = [
        f"{game_name}",
        "",
    ]

    for index, round_data in enumerate(rounds, start=1):

        player = round_data["player1"]
        bot = round_data["player2"]

        lines.append(
            f"دور {index}: "
            f"{player_name} → {player} | "
            f"🤖 ربات → {bot}"
        )

    final = rounds[-1]["result"]

    lines.append("")

    if final == "player1":
        lines.append(
            f"🏆 برنده: {player_name}"
        )

    elif final == "player2":
        lines.append(
            "🤖 برنده: ربات"
        )

    return "\n".join(lines)
