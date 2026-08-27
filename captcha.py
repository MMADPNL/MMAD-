# =========================================================
# BET BOT - CAPTCHA
# =========================================================

import random

from database import set_captcha_ok, captcha_is_ok


def create_captcha():
    a = random.randint(1, 9)
    b = random.randint(1, 9)

    answer = a + b

    options = {answer}

    while len(options) < 4:
        options.add(random.randint(2, 18))

    options = list(options)
    random.shuffle(options)

    return {
        "question": f"{a} + {b} = ؟",
        "answer": answer,
        "options": options,
    }


def save_captcha_result(user_id: int, correct: bool):
    if correct:
        set_captcha_ok(user_id, True)
        return True

    return False


def user_passed_captcha(user_id: int) -> bool:
    return captcha_is_ok(user_id)
