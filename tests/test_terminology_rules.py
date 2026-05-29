import pytest
from terminology_rules import TERMINOLOGY_RULES, apply_terminology_regex


class TestTerminologyRules:
    """Test terminology regex rules."""

    def test_rules_is_list_of_tuples(self):
        assert isinstance(TERMINOLOGY_RULES, list)
        for rule in TERMINOLOGY_RULES:
            assert len(rule) == 3, f"Rule should be (pattern, replacement, flags): {rule}"

    # --- 英文專名合併 ---
    def test_github_merge(self):
        assert apply_terminology_regex("用 Git Hub 管理") == "用 GitHub 管理"

    def test_openai_merge(self):
        assert apply_terminology_regex("Open AI 發布了") == "OpenAI 發布了"

    def test_chatgpt_merge(self):
        assert apply_terminology_regex("Chat GPT 很好用") == "ChatGPT 很好用"

    def test_case_insensitive_english(self):
        assert apply_terminology_regex("git hub") == "GitHub"
        assert apply_terminology_regex("OPEN AI") == "OpenAI"

    # --- 中文音譯 ---
    def test_token_chinese(self):
        assert apply_terminology_regex("這個偷坑數量") == "這個token數量"

    def test_jiju_chinese(self):
        assert apply_terminology_regex("市值集聚") == "市值級距"

    # --- 不該改的不改 ---
    def test_no_false_positive_git(self):
        """git alone should not be changed."""
        assert apply_terminology_regex("用 git 管理") == "用 git 管理"

    def test_no_false_positive_chinese(self):
        """Normal text should pass through unchanged."""
        assert apply_terminology_regex("今天天氣很好") == "今天天氣很好"

    def test_longest_first(self):
        """Longer patterns should match before shorter ones."""
        result = apply_terminology_regex("Chat GPT")
        assert result == "ChatGPT"
