# Discord Bot Deployment Manual (Railway + GitHub)

## Files
This project contains:

- `bot.py`
- `requirements.txt`
- `runtime.txt`

The bot reads its token from the environment variable:

`DISCORD_BOT_TOKEN`

and starts with:

`python bot.py`

## Cost
Railway Hobby costs **$5/month minimum usage** and includes **$5 of monthly usage credits**. If total usage stays at or below that amount, total cost stays **$5/month**. If usage exceeds it, only the difference is charged.

## 1. Create the Discord application
- Open the Discord Developer Portal
- Create a new application

## 2. Add a bot user
- Open the **Bot** section
- Click **Add Bot**

## 3. Copy the bot token
- Copy the token from the Bot page
- Do **not** put it in the source code
- Do **not** commit it to GitHub

## 4. Enable Message Content Intent
This bot uses message-based commands such as `!worker`.

In the Discord Developer Portal:
- Open **Bot**
- Enable **Message Content Intent**

## 5. Invite the bot to the server
Invite the bot to the target Discord server with permissions to:
- view channels
- read message history
- send messages
- embed links
- mention roles

## 6. Upload the code to GitHub
Create a GitHub repository and upload:
- `bot.py`
- `requirements.txt`
- `runtime.txt`

## 7. Create the Railway project
- Sign in to Railway
- Create a **New Project**
- Deploy from **GitHub repo**
- Select the repository

## 8. Add the token in Railway
In Railway, open the service and add this variable:

`DISCORD_BOT_TOKEN=your_actual_bot_token`

## 9. Add a volume
This bot stores data in:

`/data/worker_data.json`

Create a Railway Volume and mount it to:

`/data`

## 10. Set the start command
Set the Railway start command to:

`python bot.py`

## 11. Deploy
Deploy the service and check the logs.

A successful startup should show something like:

`Logged in as ...`
