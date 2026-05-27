this was the example to test the query when creating the APi Key:
```
curl https://api.anthropic.com/v1/messages \
        --header "x-api-key: ${{ secrets.ANTHROPIC_API_KEY }}" \
        --header "anthropic-version: 2023-06-01" \
        --header "content-type: application/json" \
        --data \
    '{
        "model": "claude-sonnet-4-6",
        "max_tokens": 1024,
        "messages": [
            {"role": "user", "content": "Hello, world"}
        ]
    }'

```
