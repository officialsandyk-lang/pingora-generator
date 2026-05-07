import os

import pytest
from langsmith import traceable
from langsmith.wrappers import wrap_openai
from openai import OpenAI


@pytest.mark.skipif(
    os.getenv("RUN_LANGSMITH_SMOKE_TEST") != "1",
    reason="LangSmith smoke test requires real OpenAI/LangSmith keys. Set RUN_LANGSMITH_SMOKE_TEST=1 to run.",
)
def test_langsmith():
    client = wrap_openai(OpenAI())

    @traceable(name="langsmith_test", run_type="chain")
    def run_trace():
        response = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
            messages=[
                {
                    "role": "user",
                    "content": "Say LangSmith tracing works in five words.",
                }
            ],
            temperature=0,
        )

        return response.choices[0].message.content

    result = run_trace()

    assert isinstance(result, str)
    assert result.strip()