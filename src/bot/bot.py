import os
import discord
from discord import Intents, Interaction, Message, ChannelType, ButtonStyle, InteractionType, ComponentType
from discord.ui import Button, View
from discord.ext.commands import Bot

intents = Intents.none()
intents.dm_messages = True
intents.guild_messages = True

client = Bot(command_prefix="!", intents=intents)

class Feedback(discord.ui.Modal, title=''):
    
    feedback = discord.ui.TextInput(
        label='Feedback',
        placeholder='How can we improve this response?',
        required=False,
        max_length=300,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_message(f'Thanks for your feedback, {self.name.value}!', ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await interaction.response.send_message('Oops! Something went wrong.', ephemeral=True)


def trunc(text: str, max: int = 20):
  return text[:max] if len(text) > max else text

async def thumbs_up(interaction: Interaction):
  await interaction.response.send_message("You clicked thumbs up!", ephemeral=True)

async def thumbs_down(interaction: Interaction):
  await interaction.response.send_modal(Feedback())


@client.event
async def on_interaction(interaction: Interaction):
  if interaction.type != InteractionType.component:
    return
  if not interaction.data:
    return
  if "component_type" not in interaction.data or interaction.data["component_type"] != ComponentType.button.value:
    return
  
  custom_id = interaction.data["custom_id"]
  
  match custom_id:
    case "thumbs_up": await thumbs_up(interaction)
    case "thumbs_down": await thumbs_down(interaction)

@client.event
async def on_message(message: Message):
  print("got msg")
  if message.author == client.user or not client.user:
    return
  
  is_dm = message.guild is None

  if not is_dm and not client.user.mentioned_in(message):
    return

  thumbsup_button = Button(label="👍", style=ButtonStyle.green, custom_id="thumbs_up")
  thumbsdown_button = Button(label="👎", style=ButtonStyle.red, custom_id="thumbs_down")

  view = View()
  view.add_item(thumbsup_button)
  view.add_item(thumbsdown_button)

  for attachment in message.attachments:
    attachment.url

  if is_dm or message.channel.type in [ChannelType.public_thread, ChannelType.private_thread]:
    await message.reply("hello world", view=view)
  else:
    thread = await message.create_thread(name=trunc(message.clean_content))
    await thread.send("hello world", view=view)

@client.event
async def on_ready():
  try:
    s = await client.tree.sync()
    print(f"Synced {len(s)} commands")
  except Exception as e:
    print(e)

@client.tree.command(name = "help")
async def help(interaction: Interaction):
  await interaction.response.send_message(f"""
Hi {interaction.user.mention}!

I'm the Solace AI Chatbot, designed to assist Solace employees with various tasks and information needs. 

## What I can do:
* Answer general questions and provide assistance
* Search Solace customer documentation for product information
* Access Confluence pages for internal documentation
* Retrieve employee information (schedules, contact details, org charts)
* Work with Jira issues and RT support tickets
* Generate diagrams, charts, and reports
* Process and analyze images
* Search the web for current information

I can help with both general inquiries and Solace-specific questions by leveraging specialized agents to access the right information sources. Just let me know what you need assistance with!""", ephemeral=True)

client.run(os.getenv('DISCORD_TOKEN') or exit(-123))
