from langsmith import traceable
from langsmith.wrappers import wrap_openai
from openai import OpenAI

client = wrap_openai(OpenAI())


@traceable(name="langsmith_test", run_type="chain")
def test_langsmith():
    response = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {
                "role": "user",
                "content": "Say LangSmith tracing works in five words.",
            }
        ],
        temperature=0,
    )

    return response.choices[0].message.content


print(test_langsmith())