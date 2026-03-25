import re


class Validators:
    @staticmethod
    def is_valid_email(email: str) -> bool:
        pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        return bool(re.match(pattern, email))

    @staticmethod
    def is_valid_username(username: str) -> bool:
        pattern = r'^[a-zA-Z0-9_]{4,20}$'
        return bool(re.match(pattern, username))

    @staticmethod
    def is_valid_password(password: str) -> bool:
        return len(password) >= 6

    @staticmethod
    def is_valid_phone(phone: str) -> bool:
        pattern = r'^1[3-9]\d{9}$'
        return bool(re.match(pattern, phone))
