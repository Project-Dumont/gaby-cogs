import asyncio
import re
import uuid
from pathlib import Path

import discord
from redbot.core import checks, commands, Config
from redbot.core.data_manager import cog_data_path
from red_commons.logging import getLogger

log = getLogger("red.gaby-cogs.rebase")

ANSWER_TIMEOUT = 120
MAX_TAGS = 20
HEX_COLOR = re.compile(r"^#?([0-9a-fA-F]{6})$")


def key_of(name: str) -> str:
    return name.strip().lower()


class CodebaseForm(discord.ui.Modal, title="Codebase"):
    # tag de fórum aceita no maximo 20 caracteres
    name = discord.ui.TextInput(label="Nome da codebase", placeholder="Wizden", max_length=20)
    color = discord.ui.TextInput(label="Cor do cargo (hex)", placeholder="#ff8800", min_length=6, max_length=7)
    description = discord.ui.TextInput(label="Descrição", style=discord.TextStyle.paragraph, max_length=2000)
    github = discord.ui.TextInput(label="Link do repositorio", placeholder="https://github.com/space-wizards/space-station-14")

    def __init__(self, current: dict | None = None):
        super().__init__(timeout=600)
        self.submitted = False

        if current is not None:
            self.name.default = current["name"]
            self.color.default = f'#{current["color"]}'
            self.description.default = current["description"]
            self.github.default = current["github"]

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.submitted = True
        self.stop()


class FormButton(discord.ui.View):
    def __init__(self, member, current: dict | None = None):
        super().__init__(timeout=ANSWER_TIMEOUT)
        self.member = member
        self.current = current
        self.modal = None

    @discord.ui.button(label="Preencher", style=discord.ButtonStyle.green)
    async def fill(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.member != interaction.user:
            return await interaction.response.send_message("Apenas quem usou o comando pode preencher", ephemeral=True)

        self.modal = CodebaseForm(self.current)
        await interaction.response.send_modal(self.modal)
        await self.modal.wait()
        self.stop()


class RejectReason(discord.ui.Modal, title="Recusar codebase"):
    reason = discord.ui.TextInput(label="Motivo", style=discord.TextStyle.paragraph, max_length=1000)

    def __init__(self, request_id: str):
        super().__init__()
        self.request_id = request_id

    async def on_submit(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("Rebase")
        await cog.reject(interaction, self.request_id, self.reason.value)


# os botões são DynamicItem pra continuarem funcionando depois que o bot reinicia
class ApproveButton(discord.ui.DynamicItem[discord.ui.Button], template=r"rebase:approve:(?P<id>\w+)"):
    def __init__(self, request_id: str):
        super().__init__(discord.ui.Button(
            label="Aprovar",
            style=discord.ButtonStyle.green,
            custom_id=f"rebase:approve:{request_id}"
        ))
        self.request_id = request_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["id"])

    async def callback(self, interaction: discord.Interaction):
        await interaction.client.get_cog("Rebase").approve(interaction, self.request_id)


class RejectButton(discord.ui.DynamicItem[discord.ui.Button], template=r"rebase:reject:(?P<id>\w+)"):
    def __init__(self, request_id: str):
        super().__init__(discord.ui.Button(
            label="Recusar",
            style=discord.ButtonStyle.red,
            custom_id=f"rebase:reject:{request_id}"
        ))
        self.request_id = request_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["id"])

    async def callback(self, interaction: discord.Interaction):
        if not await interaction.client.is_admin(interaction.user):
            return await interaction.response.send_message("Apenas um admin pode recusar isso", ephemeral=True)

        await interaction.response.send_modal(RejectReason(self.request_id))


class TransferButton(discord.ui.DynamicItem[discord.ui.Button], template=r"rebase:transfer:(?P<accept>yes|no):(?P<old>\d+):(?P<new>\d+)"):
    def __init__(self, accept: bool, old_id: int, new_id: int):
        super().__init__(discord.ui.Button(
            label="Aceitar" if accept else "Recusar",
            style=discord.ButtonStyle.green if accept else discord.ButtonStyle.red,
            custom_id=f'rebase:transfer:{"yes" if accept else "no"}:{old_id}:{new_id}'
        ))
        self.accept = accept
        self.old_id = old_id
        self.new_id = new_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["accept"] == "yes", int(match["old"]), int(match["new"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.client.get_cog("Rebase").answer_transfer(interaction, self.accept, self.old_id, self.new_id)


class Rebase(commands.Cog):
    """Periodo votação de rebase do servidor!"""

    def __init__(self, bot, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.config = Config.get_conf(self, identifier=1116202614)

        default_guild = {
            "forum_id": None,
            "approval_channel_id": None,
            "rep_role_id": None,
            "codebases": {},
            "pending": {},
        }

        self.config.register_guild(**default_guild, force_registration=True)
        self.bot = bot

        # o link das imagens do discord expira, entao a gente guarda o arquivo aqui
        self.images = cog_data_path(self) / "images"
        self.images.mkdir(parents=True, exist_ok=True)

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(ApproveButton, RejectButton, TransferButton)

    async def cog_unload(self) -> None:
        self.bot.remove_dynamic_items(ApproveButton, RejectButton, TransferButton)

    def image_file(self, filename: str) -> discord.File:
        return discord.File(self.images / filename, filename=filename)

    def delete_image(self, filename: str) -> None:
        (self.images / filename).unlink(missing_ok=True)

    async def save_image(self, attachment: discord.Attachment) -> str:
        filename = uuid.uuid4().hex + (Path(attachment.filename).suffix.lower() or ".png")
        await attachment.save(self.images / filename)
        return filename

    def codebase_embed(self, codebase: dict) -> discord.Embed:
        embed = discord.Embed(
            title=codebase["name"],
            description=codebase["description"],
            color=int(codebase["color"], 16)
        )
        embed.add_field(name="Repositorio", value=codebase["github"], inline=False)

        rep = codebase["representative_id"]
        embed.add_field(name="Representante", value=f"<@{rep}>" if rep else "Sem representante")

        if codebase.get("status") == "disqualified":
            embed.add_field(name="Status", value="Desqualificada")

        embed.set_thumbnail(url=f'attachment://{codebase["image"]}')
        return embed

    async def get_setup(self, ctx: commands.Context):
        settings = await self.config.guild(ctx.guild).all()

        forum = ctx.guild.get_channel(settings["forum_id"] or 0)
        approval = ctx.guild.get_channel(settings["approval_channel_id"] or 0)
        rep_role = ctx.guild.get_role(settings["rep_role_id"] or 0)

        if forum is None or approval is None or rep_role is None:
            await ctx.send(f"o cog ainda nao foi configurado! `{ctx.clean_prefix}rebase setup`")
            return None

        return forum, approval, rep_role

    async def get_thread(self, thread_id: int | None) -> discord.Thread | None:
        if thread_id is None:
            return None

        try:
            return await self.bot.fetch_channel(thread_id)
        except discord.NotFound:
            return None

    async def find_codebase(self, ctx: commands.Context, name: str, active_only: bool = True) -> dict | None:
        codebase = (await self.config.guild(ctx.guild).codebases()).get(key_of(name))

        if codebase is None:
            await ctx.send("Não achei nenhuma codebase com esse nome!")
            return None

        if active_only and codebase["status"] != "active":
            await ctx.send(f'A **{codebase["name"]}** foi desqualificada')
            return None

        return codebase

    async def represented_by(self, guild: discord.Guild, member_id: int) -> dict | None:
        for codebase in (await self.config.guild(guild).codebases()).values():
            if codebase["status"] == "active" and codebase["representative_id"] == member_id:
                return codebase

        return None

    async def save_codebase(self, guild: discord.Guild, codebase: dict) -> None:
        await self.config.guild(guild).codebases.set_raw(key_of(codebase["name"]), value=codebase)

    async def update_post(self, guild: discord.Guild, codebase: dict) -> None:
        thread = await self.get_thread(codebase["thread_id"])

        if thread is None:
            log.warning(f'Post da codebase {codebase["name"]} sumiu.')
            return

        # nao da pra editar mensagem em post arquivado
        if thread.archived and not thread.locked:
            await thread.edit(archived=False)

        message = await thread.fetch_message(thread.id)
        await message.edit(embed=self.codebase_embed(codebase), attachments=[self.image_file(codebase["image"])])

    async def delete_tag(self, guild: discord.Guild, tag_id: int | None) -> None:
        forum = guild.get_channel(await self.config.guild(guild).forum_id())

        if forum is None or tag_id is None:
            return

        await forum.edit(available_tags=[tag for tag in forum.available_tags if tag.id != tag_id])

    async def set_rep_role(self, guild: discord.Guild, member_id: int | None, give: bool) -> None:
        rep_role = guild.get_role(await self.config.guild(guild).rep_role_id())
        member = guild.get_member(member_id or 0)

        if rep_role is None or member is None:
            return

        if give:
            await member.add_roles(rep_role, reason="Candidatura: virou representante")
        else:
            await member.remove_roles(rep_role, reason="Candidatura: deixou de ser representante")

    async def ask(self, ctx: commands.Context, question: str) -> discord.Message | None:
        await ctx.send(f"{question}\n-# manda `cancelar` pra parar")

        def check(message: discord.Message):
            return message.author == ctx.author and message.channel == ctx.channel

        try:
            answer = await self.bot.wait_for("message", check=check, timeout=ANSWER_TIMEOUT)
        except asyncio.TimeoutError:
            await ctx.send("demorou demais...")
            return None

        if answer.content.strip().lower() == "cancelar":
            await ctx.send("cancelado")
            return None

        return answer

    async def check_form(self, guild: discord.Guild, data: dict, ignore: str | None = None) -> str | None:
        if not data["name"]:
            return "o nome nao pode ficar vazio"

        name = key_of(data["name"])
        codebases = await self.config.guild(guild).codebases()
        pending = await self.config.guild(guild).pending()

        taken = [key for key in codebases if key != ignore]
        taken += [key_of(request["name"]) for request in pending.values()]

        if name in taken:
            return "ja tem uma codebase com esse nome!"

        if not HEX_COLOR.match(data["color"]):
            return "essa cor nao é um hex valido. Ex: `#ff8800`"

        if not data["github"].startswith(("https://github.com/", "http://github.com/")):
            return "o link do repositorio deve ser do github"

        return None

    async def fill_form(self, ctx: commands.Context, current: dict | None = None) -> dict | None:
        view = FormButton(ctx.author, current)
        await ctx.send("Preencha as informações da codebase pelo botão para continuar.", view=view)
        await view.wait()

        if view.modal is None or not view.modal.submitted:
            return None

        form = view.modal
        data = {
            "name": form.name.value.strip(),
            "color": form.color.value.strip(),
            "description": form.description.value.strip(),
            "github": form.github.value.strip(),
        }

        error = await self.check_form(ctx.guild, data, key_of(current["name"]) if current else None)
        if error:
            await ctx.send(error)
            return None

        data["color"] = HEX_COLOR.match(data["color"]).group(1).lower()

        question = "Mande uma imagem para servir de thumbnail da codebase. (16:9)"
        if current is not None:
            question += ", ou manda `pular` pra manter a atual"

        while True:
            answer = await self.ask(ctx, question)
            if answer is None:
                return None

            if current is not None and answer.content.strip().lower() == "pular":
                data["image"] = current["image"]
                return data

            images = [a for a in answer.attachments if a.content_type and a.content_type.startswith("image/")]
            if images:
                data["image"] = await self.save_image(images[0])
                return data

            await ctx.send("isso nao é uma imagem! lol")

    async def create_codebase(self, guild: discord.Guild, data: dict, representative: discord.Member | None) -> dict:
        forum = guild.get_channel(await self.config.guild(guild).forum_id())

        if len(forum.available_tags) >= MAX_TAGS:
            raise commands.UserFeedbackCheckFailure(f"Sem tags sobrando para o forum! Ficando de fora...")

        codebase = {
            "name": data["name"],
            "color": data["color"],
            "description": data["description"],
            "github": data["github"],
            "image": data["image"],
            "representative_id": representative.id if representative else None,
            "status": "active",
        }

        role = await guild.create_role(name=codebase["name"], color=discord.Color(int(codebase["color"], 16)), mentionable=True, reason="Rebase: nova codebase")
        tag = await forum.create_tag(name=codebase["name"])
        post = await forum.create_thread(
            name=codebase["name"],
            embed=self.codebase_embed(codebase),
            file=self.image_file(codebase["image"]),
            applied_tags=[tag]
        )

        codebase["role_id"] = role.id
        codebase["tag_id"] = tag.id
        codebase["thread_id"] = post.thread.id
        await self.save_codebase(guild, codebase)

        if representative is not None:
            await representative.add_roles(role, reason="Candidatura: criou uma codebase")
            await self.set_rep_role(guild, representative.id, True)

        return codebase

    async def approve(self, interaction: discord.Interaction, request_id: str) -> None:
        guild = interaction.guild

        if not await self.bot.is_admin(interaction.user):
            return await interaction.response.send_message("Apenas a staff pode aprovar!", ephemeral=True)

        async with self.config.guild(guild).pending() as pending:
            request = pending.pop(request_id, None)

        if request is None:
            return await interaction.response.send_message("esse pedido nao existe mais!", ephemeral=True)

        await interaction.response.defer()

        member = guild.get_member(request["representative_id"])
        if member is None or await self.represented_by(guild, member.id):
            self.delete_image(request["image"])
            await interaction.message.edit(content="pedido cancelado, o jogador saiu do servidor ou ja representa outra codebase", view=None)
            return

        try:
            codebase = await self.create_codebase(guild, request, member)
        except Exception as e:
            # devolve o pedido pra dar pra tentar de novo
            await self.config.guild(guild).pending.set_raw(request_id, value=request)

            if isinstance(e, commands.UserFeedbackCheckFailure):
                await interaction.followup.send(e.message, ephemeral=True)
            else:
                log.exception(f'Erro criando a codebase {request["name"]}.')
                await interaction.followup.send("Erro criando a codebase, olhe o console para mais informações.", ephemeral=True)
            return

        await interaction.message.edit(content=f"Aprovada por {interaction.user.mention}: <#{codebase['thread_id']}>", view=None)

        try:
            await member.send(f'sua codebase **{codebase["name"]}** foi aprovada! <#{codebase["thread_id"]}>')
        except discord.HTTPException:
            pass

    async def reject(self, interaction: discord.Interaction, request_id: str, reason: str) -> None:
        async with self.config.guild(interaction.guild).pending() as pending:
            request = pending.pop(request_id, None)

        if request is None:
            return await interaction.response.send_message("EsTe pedido nao existe mais!", ephemeral=True)

        self.delete_image(request["image"])
        await interaction.response.edit_message(content=f"Recusada por {interaction.user.mention}: {reason}", view=None)

        member = interaction.guild.get_member(request["representative_id"])
        if member is None:
            return

        try:
            await member.send(f'sua codebase **{request["name"]}** foi recusada.\nmotivo: {reason}')
        except discord.HTTPException:
            pass

    async def answer_transfer(self, interaction: discord.Interaction, accept: bool, old_id: int, new_id: int) -> None:
        guild = interaction.guild

        if interaction.user.id != new_id:
            return await interaction.response.send_message("isso nao é pra vc 💔", ephemeral=True)

        if not accept:
            return await interaction.response.edit_message(content=f"{interaction.user.mention} recusou a representação", view=None)

        codebase = await self.represented_by(guild, old_id)
        role = guild.get_role(codebase["role_id"]) if codebase else None

        if role is None or role not in interaction.user.roles or await self.represented_by(guild, new_id):
            return await interaction.response.edit_message(content="Essa transferencia não vale mais!", view=None)

        codebase["representative_id"] = new_id
        await self.save_codebase(guild, codebase)
        await interaction.response.edit_message(content=f'agora {interaction.user.mention} é o representante da **{codebase["name"]}**!', view=None)

        await self.set_rep_role(guild, old_id, False)
        await self.set_rep_role(guild, new_id, True)
        await self.update_post(guild, codebase)

    @commands.hybrid_group()
    @commands.guild_only()
    async def rebase(self, ctx: commands.Context) -> None:
        """
        Comandos do período de votação do rebase.
        """
        pass

    @rebase.command()
    @checks.admin()
    async def setup(self, ctx: commands.Context) -> None:
        """
        Configura o fórum, o canal de aprovação e o cargo de representante.
        """

        def first_id(text: str) -> int:
            found = re.findall(r"\d{17,20}", text)
            return int(found[0]) if found else 0

        while True:
            answer = await self.ask(ctx, "qual o forum onde os posts das codebases vao ficar?")
            if answer is None:
                return

            forum = ctx.guild.get_channel(first_id(answer.content))
            if isinstance(forum, discord.ForumChannel):
                break

            await ctx.send("isso nao é um forum")

        while True:
            answer = await self.ask(ctx, "qual canal os pedidos vao chegar pra aprovação?")
            if answer is None:
                return

            approval = ctx.guild.get_channel(first_id(answer.content))
            if isinstance(approval, discord.TextChannel):
                break

            await ctx.send("isso nao é um canal de texto")

        while True:
            answer = await self.ask(ctx, "qual o cargo de representante?")
            if answer is None:
                return

            rep_role = ctx.guild.get_role(first_id(answer.content))
            if rep_role is not None:
                break

            await ctx.send("não achei esse cargo!")

        guild_settings = self.config.guild(ctx.guild)
        await guild_settings.forum_id.set(forum.id)
        await guild_settings.approval_channel_id.set(approval.id)
        await guild_settings.rep_role_id.set(rep_role.id)

        await ctx.send(f"pronto! forum: {forum.mention}, aprovação: {approval.mention}, representante: **{rep_role.name}**.")

    @rebase.command()
    async def create(self, ctx: commands.Context) -> None:
        """
        Propõe uma codebase pra você representar.
        """
        setup = await self.get_setup(ctx)
        if setup is None:
            return

        _, approval, _ = setup

        async def blocked() -> bool:
            if await self.represented_by(ctx.guild, ctx.author.id):
                await ctx.send("Você ja representa uma codebase!")
                return True

            pending = await self.config.guild(ctx.guild).pending()
            if any(request["representative_id"] == ctx.author.id for request in pending.values()):
                await ctx.send("você ja tem um pedido esperando aprovação...")
                return True

            return False

        if await blocked():
            return

        data = await self.fill_form(ctx)
        if data is None:
            return

        if await blocked():
            self.delete_image(data["image"])
            return

        request_id = uuid.uuid4().hex[:16]
        data["representative_id"] = ctx.author.id

        view = discord.ui.View(timeout=None)
        view.add_item(ApproveButton(request_id))
        view.add_item(RejectButton(request_id))

        await approval.send(
            content=f"Nova codebase proposta por {ctx.author.mention}",
            embed=self.codebase_embed(data),
            file=self.image_file(data["image"]),
            view=view
        )

        await self.config.guild(ctx.guild).pending.set_raw(request_id, value=data)
        await ctx.send("pedido enviado para aprovação.")

    @rebase.command()
    async def join(self, ctx: commands.Context, *, codebase: str) -> None:
        """
        Entra em uma codebase.
        """
        found = await self.find_codebase(ctx, codebase)
        if found is None:
            return

        role = ctx.guild.get_role(found["role_id"])
        if role in ctx.author.roles:
            await ctx.send("vc ja ta nessa codebase brother")
            return

        await ctx.author.add_roles(role, reason="Rebase: entrou na codebase")
        await ctx.send(f'Agora vc faz parte do(a) **{found["name"]}**!')

    @rebase.command()
    async def leave(self, ctx: commands.Context, *, codebase: str) -> None:
        """
        Sai de uma codebase.
        """
        found = await self.find_codebase(ctx, codebase)
        if found is None:
            return

        if found["representative_id"] == ctx.author.id:
            await ctx.send(f"Você é o representante desta codebase! Ultilize `{ctx.clean_prefix}rebase transfer` ou `{ctx.clean_prefix}rebase resign` antes.")
            return

        role = ctx.guild.get_role(found["role_id"])
        if role not in ctx.author.roles:
            await ctx.send("Você não está nessa codebase.")
            return

        await ctx.author.remove_roles(role, reason="Rebase: saiu da codebase")
        await ctx.send(f'Você saiu da **{found["name"]}**.')

    @rebase.command()
    async def list(self, ctx: commands.Context) -> None:
        """
        Lista as codebases candidatas.
        """
        codebases = await self.config.guild(ctx.guild).codebases()

        if len(codebases) == 0:
            await ctx.send("Nenhuma codebase cadastrada ainda.")
            return

        active = []
        disqualified = []

        for codebase in codebases.values():
            if codebase["status"] != "active":
                disqualified.append(f'~~{codebase["name"]}~~')
                continue

            role = ctx.guild.get_role(codebase["role_id"])
            members = len(role.members) if role else 0
            rep = f'<@{codebase["representative_id"]}>' if codebase["representative_id"] else "Sem representante"
            active.append(f'**{codebase["name"]}**: {rep}, {members} membros')

        embed = discord.Embed(title="Codebases candidatas", description="\n".join(active) or "Nenhuma.")

        if disqualified:
            embed.add_field(name="Desqualificadas", value=", ".join(disqualified)[:1024])

        await ctx.send(embed=embed)

    @rebase.command()
    async def info(self, ctx: commands.Context, *, codebase: str) -> None:
        """
        Mostra as infos de uma codebase.
        """
        found = await self.find_codebase(ctx, codebase, active_only=False)
        if found is None:
            return

        embed = self.codebase_embed(found)

        role = ctx.guild.get_role(found["role_id"] or 0)
        if role is not None:
            embed.add_field(name="Membros", value=str(len(role.members)))
            embed.add_field(name="Post", value=f'<#{found["thread_id"]}>')

        await ctx.send(embed=embed, file=self.image_file(found["image"]))

    @rebase.command()
    async def edit(self, ctx: commands.Context, *, codebase: str = None) -> None:
        """
        Edita a codebase que você representa.
        """
        if codebase is not None:
            if not await self.bot.is_admin(ctx.author):
                await ctx.send("Apenas admins podem editar a codebase de outra pessoa.")
                return

            found = await self.find_codebase(ctx, codebase)
        else:
            found = await self.represented_by(ctx.guild, ctx.author.id)
            if found is None:
                await ctx.send("Você não representa nenhuma codebase.")

        if found is None:
            return

        data = await self.fill_form(ctx, found)
        if data is None:
            return

        old_name = found["name"]
        old_image = found["image"]
        edited = {**found, **data}

        role = ctx.guild.get_role(edited["role_id"])
        if role is not None:
            await role.edit(name=edited["name"], color=discord.Color(int(edited["color"], 16)))

        if edited["name"] != old_name:
            forum = ctx.guild.get_channel(await self.config.guild(ctx.guild).forum_id())
            tags = forum.available_tags

            for tag in tags:
                if tag.id == edited["tag_id"]:
                    tag.name = edited["name"]

            await forum.edit(available_tags=tags)

            thread = await self.get_thread(edited["thread_id"])
            if thread is not None:
                await thread.edit(name=edited["name"])

            await self.config.guild(ctx.guild).codebases.clear_raw(key_of(old_name))

        await self.save_codebase(ctx.guild, edited)
        await self.update_post(ctx.guild, edited)

        if edited["image"] != old_image:
            self.delete_image(old_image)

        await ctx.send(f'**{edited["name"]}** atualizada!')

    @rebase.command()
    async def transfer(self, ctx: commands.Context, member: discord.Member) -> None:
        """
        Passa a representação da sua codebase para outra pessoa que está nela.
        """
        codebase = await self.represented_by(ctx.guild, ctx.author.id)
        if codebase is None:
            await ctx.send("Você não representa nenhuma codebase.")
            return

        if member == ctx.author or member.bot:
            await ctx.send("Não é possível passar a representação para essa pessoa.")
            return

        role = ctx.guild.get_role(codebase["role_id"])
        if role not in member.roles:
            await ctx.send(f'{member.display_name} precisa estar na **{codebase["name"]}** primeiro.')
            return

        if await self.represented_by(ctx.guild, member.id):
            await ctx.send(f"{member.display_name} já representa outra codebase.")
            return

        view = discord.ui.View(timeout=None)
        view.add_item(TransferButton(True, ctx.author.id, member.id))
        view.add_item(TransferButton(False, ctx.author.id, member.id))

        await ctx.send(f'{member.mention}, {ctx.author.mention} quer passar para você a representação da **{codebase["name"]}**. Você aceita?', view=view)

    @rebase.command()
    async def resign(self, ctx: commands.Context) -> None:
        """
        Larga a representação da sua codebase.
        """
        codebase = await self.represented_by(ctx.guild, ctx.author.id)
        if codebase is None:
            await ctx.send("Você não representa nenhuma codebase.")
            return

        codebase["representative_id"] = None
        await self.save_codebase(ctx.guild, codebase)
        await self.set_rep_role(ctx.guild, ctx.author.id, False)
        await self.update_post(ctx.guild, codebase)

        await ctx.send(f'Você não é mais representante da **{codebase["name"]}**. Agora qualquer membro dela pode usar `{ctx.clean_prefix}rebase claim {codebase["name"]}` para assumir o cargo.')

    @rebase.command()
    async def claim(self, ctx: commands.Context, *, codebase: str) -> None:
        """
        Assume a representação de uma codebase sem representante.
        """
        found = await self.find_codebase(ctx, codebase)
        if found is None:
            return

        if found["representative_id"] is not None:
            await ctx.send("Essa codebase já tem um representante.")
            return

        role = ctx.guild.get_role(found["role_id"])
        if role not in ctx.author.roles:
            await ctx.send(f'Entre nela primeiro com `{ctx.clean_prefix}rebase join {found["name"]}`.')
            return

        if await self.represented_by(ctx.guild, ctx.author.id):
            await ctx.send("Você já representa outra codebase.")
            return

        found["representative_id"] = ctx.author.id
        await self.save_codebase(ctx.guild, found)
        await self.set_rep_role(ctx.guild, ctx.author.id, True)
        await self.update_post(ctx.guild, found)

        await ctx.send(f'Agora você é o representante da **{found["name"]}**!')

    @rebase.command()
    @checks.admin()
    async def add(self, ctx: commands.Context) -> None:
        """
        Cria uma codebase sem representante, sem passar pela aprovação.
        """
        if await self.get_setup(ctx) is None:
            return

        data = await self.fill_form(ctx)
        if data is None:
            return

        try:
            codebase = await self.create_codebase(ctx.guild, data, None)
        except commands.UserFeedbackCheckFailure as e:
            self.delete_image(data["image"])
            await ctx.send(e.message)
            return

        await ctx.send(f'Codebase **{codebase["name"]}** criada: <#{codebase["thread_id"]}>.')

    @rebase.command()
    @checks.admin()
    async def remove(self, ctx: commands.Context, *, codebase: str) -> None:
        """
        Apaga uma codebase: cargo, post, tag e tudo.
        """
        found = await self.find_codebase(ctx, codebase, active_only=False)
        if found is None:
            return

        async with ctx.typing():
            role = ctx.guild.get_role(found["role_id"] or 0)
            if role is not None:
                await role.delete(reason="Rebase: codebase removida")

            thread = await self.get_thread(found["thread_id"])
            if thread is not None:
                await thread.delete()

            await self.delete_tag(ctx.guild, found["tag_id"])

            if found["status"] == "active":
                await self.set_rep_role(ctx.guild, found["representative_id"], False)

            await self.config.guild(ctx.guild).codebases.clear_raw(key_of(found["name"]))
            self.delete_image(found["image"])

        await ctx.send(f'Codebase **{found["name"]}** removida.')

    @rebase.command()
    @checks.admin()
    async def disqualify(self, ctx: commands.Context, *, codebase: str) -> None:
        """
        Desqualifica uma codebase: apaga o cargo e a tag, e arquiva o post.
        """
        found = await self.find_codebase(ctx, codebase)
        if found is None:
            return

        async with ctx.typing():
            found["status"] = "disqualified"
            await self.update_post(ctx.guild, found)

            role = ctx.guild.get_role(found["role_id"])
            if role is not None:
                await role.delete(reason="Rebase: codebase desqualificada")

            await self.delete_tag(ctx.guild, found["tag_id"])
            await self.set_rep_role(ctx.guild, found["representative_id"], False)

            thread = await self.get_thread(found["thread_id"])
            if thread is not None:
                await thread.edit(archived=True, locked=True)

            found["role_id"] = None
            found["tag_id"] = None
            await self.save_codebase(ctx.guild, found)

        await ctx.send(f'A **{found["name"]}** foi desqualificada.')
