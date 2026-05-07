import os
from langsmith import Client

client = Client(
    api_key=os.environ["LANGSMITH_API_KEY"],
    api_url=os.environ["LANGSMITH_ENDPOINT"],
)

print("Endpoint:", os.environ["LANGSMITH_ENDPOINT"])
print("Project:", os.environ.get("LANGSMITH_PROJECT"))

projects = list(client.list_projects())
print("✅ Auth works. Projects:")
for project in projects[:10]:
    print("-", project.name)