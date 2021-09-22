# -*- coding: utf-8 -*-

"""
Copyright (c) 2019 Valentin B.
A simple music bot written in discord.py using youtube-dl.
Though it's a simple example, music bots are complex and require much time and knowledge until they work perfectly.
Use this as an example or a base for your own bot and extend it as you want. If there are any bugs, please let me know.
Requirements:
Python 3.5+
pip install -U discord.py pynacl youtube-dl
You also need FFmpeg in your PATH environment variable or the FFmpeg.exe binary in your bot's directory on Windows.
"""

import asyncio
import functools
import itertools
import math
import random
import os

import discord
import youtube_dl
from async_timeout import timeout
from discord.ext import commands

import configparser

# Silence useless bug reports messages
youtube_dl.utils.bug_reports_message = lambda: ''


class VoiceError(Exception):
    pass


class YTDLError(Exception):
    pass


class YTDLSource(discord.PCMVolumeTransformer):
    YTDL_OPTIONS = {
        'format': 'bestaudio/best',
        'extractaudio': True,
        'audioformat': 'mp3',
        'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
        'restrictfilenames': True,
        'noplaylist': True,
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'logtostderr': False,
        'quiet': True,
        'no_warnings': True,
        'default_search': 'auto',
        'source_address': '0.0.0.0',
    }

    FFMPEG_OPTIONS = {
        'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
        'options': '-vn'
    }

    ytdl = youtube_dl.YoutubeDL(YTDL_OPTIONS)

    def __init__(self, ctx: commands.Context, source: discord.FFmpegPCMAudio, *, data: dict, volume: float = 0.5):
        super().__init__(source, volume)

        self.requester = ctx.author
        self.channel = ctx.channel
        self.data = data

        self.uploader = data.get('uploader')
        self.uploader_url = data.get('uploader_url')
        date = data.get('upload_date')
        self.upload_date = date[6:8] + '.' + date[4:6] + '.' + date[0:4]
        self.title = data.get('title')
        self.thumbnail = data.get('thumbnail')
        self.description = data.get('description')
        self.duration = self.parse_duration(int(data.get('duration')))
        self.tags = data.get('tags')
        self.url = data.get('webpage_url')
        self.views = data.get('view_count')
        self.likes = data.get('like_count')
        self.dislikes = data.get('dislike_count')
        self.stream_url = data.get('url')

    def __str__(self):
        return '**{0.title}** by **{0.uploader}**'.format(self)

    @classmethod
    async def create_source(cls, ctx: commands.Context, search: str, *, loop: asyncio.BaseEventLoop = None):
        loop = loop or asyncio.get_event_loop()

        partial = functools.partial(cls.ytdl.extract_info, search, download=False, process=False)
        data = await loop.run_in_executor(None, partial)

        if data is None:
            raise YTDLError('Couldn\'t find anything that matches `{}`'.format(search))

        if 'entries' not in data:
            process_info = data
        else:
            process_info = None
            for entry in data['entries']:
                if entry:
                    process_info = entry
                    break

            if process_info is None:
                raise YTDLError('Couldn\'t find anything that matches `{}`'.format(search))

        webpage_url = process_info['webpage_url']
        partial = functools.partial(cls.ytdl.extract_info, webpage_url, download=False)
        processed_info = await loop.run_in_executor(None, partial)

        if processed_info is None:
            raise YTDLError('Couldn\'t fetch `{}`'.format(webpage_url))

        if 'entries' not in processed_info:
            info = processed_info
        else:
            info = None
            while info is None:
                try:
                    info = processed_info['entries'].pop(0)
                except IndexError:
                    raise YTDLError('Couldn\'t retrieve any matches for `{}`'.format(webpage_url))

        return cls(ctx, discord.FFmpegPCMAudio(info['url'], **cls.FFMPEG_OPTIONS), data=info)

    @staticmethod
    def parse_duration(duration: int):
        minutes, seconds = divmod(duration, 60)
        hours, minutes = divmod(minutes, 60)
        days, hours = divmod(hours, 24)

        duration = []
        if days > 0:
            duration.append('{} days'.format(days))
        if hours > 0:
            duration.append('{} hours'.format(hours))
        if minutes > 0:
            duration.append('{} minutes'.format(minutes))
        if seconds > 0:
            duration.append('{} seconds'.format(seconds))

        return ', '.join(duration)


class Song:
    __slots__ = ('source', 'requester')

    def __init__(self, source: YTDLSource):
        self.source = source
        self.requester = source.requester

    def create_embed(self):
        embed = (discord.Embed(title='Now playing',
                               description='```css\n{0.source.title}\n```'.format(self),
                               color=discord.Color.blurple())
                 .add_field(name='Duration', value=self.source.duration)
                 .add_field(name='Requested by', value=self.requester.mention)
                 .add_field(name='Uploader', value='[{0.source.uploader}]({0.source.uploader_url})'.format(self))
                 .add_field(name='URL', value='[Click]({0.source.url})'.format(self))
                 .set_thumbnail(url=self.source.thumbnail))

        return embed


class SongQueue(asyncio.Queue):
    def __getitem__(self, item):
        if isinstance(item, slice):
            return list(itertools.islice(self._queue, item.start, item.stop, item.step))
        else:
            return self._queue[item]

    def __iter__(self):
        return self._queue.__iter__()

    def __len__(self):
        return self.qsize()

    def clear(self):
        self._queue.clear()

    def shuffle(self):
        random.shuffle(self._queue)

    def remove(self, index: int):
        del self._queue[index]


class VoiceState:
    def __init__(self, bot: commands.Bot, ctx: commands.Context):
        self.bot = bot
        self._ctx = ctx

        self.current = None
        self.voice = None
        self.next = asyncio.Event()
        self.songs = SongQueue()

        self._loop = False
        self._volume = 0.5
        self.skip_votes = set()

        self.audio_player = bot.loop.create_task(self.audio_player_task())

    def __del__(self):
        self.audio_player.cancel()

    @property
    def loop(self):
        return self._loop

    @loop.setter
    def loop(self, value: bool):
        self._loop = value

    @property
    def volume(self):
        return self._volume

    @volume.setter
    def volume(self, value: float):
        self._volume = value

    @property
    def is_playing(self):
        return self.voice and self.current

    async def audio_player_task(self):
        while True:
            self.next.clear()

            if not self.loop:
                # Try to get the next song within 3 minutes.
                # If no song will be added to the queue in time,
                # the player will disconnect due to performance
                # reasons.
                try:
                    async with timeout(180):  # 3 minutes
                        self.current = await self.songs.get()
                except asyncio.TimeoutError:
                    self.bot.loop.create_task(self.stop())
                    return

            self.current.source.volume = self._volume
            self.voice.play(self.current.source, after=self.play_next_song)
            await self.current.source.channel.send(embed=self.current.create_embed())

            await self.next.wait()

    def play_next_song(self, error=None):
        if error:
            raise VoiceError(str(error))

        self.next.set()

    def skip(self):
        self.skip_votes.clear()

        if self.is_playing:
            self.voice.stop()

    async def stop(self):
        self.songs.clear()

        if self.voice:
            await self.voice.disconnect()
            self.voice = None


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.voice_states = {}

    def get_voice_state(self, ctx: commands.Context):
        state = self.voice_states.get(ctx.guild.id)
        if not state:
            state = VoiceState(self.bot, ctx)
            self.voice_states[ctx.guild.id] = state

        return state

    def cog_unload(self):
        for state in self.voice_states.values():
            self.bot.loop.create_task(state.stop())

    def cog_check(self, ctx: commands.Context):
        if not ctx.guild:
            raise commands.NoPrivateMessage('This command can\'t be used in DM channels.')

        return True

    async def cog_before_invoke(self, ctx: commands.Context):
        ctx.voice_state = self.get_voice_state(ctx)

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        await ctx.send('An error occurred: {}'.format(str(error)))

    @commands.command(name='join', invoke_without_subcommand=True)
    async def _join(self, ctx: commands.Context):
        """Joins a voice channel."""

        destination = ctx.author.voice.channel
        if ctx.voice_state.voice:
            await ctx.voice_state.voice.move_to(destination)
            return

        ctx.voice_state.voice = await destination.connect()

    @commands.command(name='summon')
    @commands.has_permissions(manage_guild=True)
    async def _summon(self, ctx: commands.Context, *, channel: discord.VoiceChannel = None):
        """Summons the bot to a voice channel.
        If no channel was specified, it joins your channel.
        """

        if not channel and not ctx.author.voice:
            raise VoiceError('You are neither connected to a voice channel nor specified a channel to join.')

        destination = channel or ctx.author.voice.channel
        if ctx.voice_state.voice:
            await ctx.voice_state.voice.move_to(destination)
            return

        ctx.voice_state.voice = await destination.connect()

    @commands.command(name='leave', aliases=['disconnect'])
    @commands.has_permissions(manage_guild=True)
    async def _leave(self, ctx: commands.Context):
        """Clears the queue and leaves the voice channel."""

        if not ctx.voice_state.voice:
            return await ctx.send('Not connected to any voice channel.')

        await ctx.voice_state.stop()
        del self.voice_states[ctx.guild.id]

    @commands.command(name='volume')
    async def _volume(self, ctx: commands.Context, *, volume: int):
        """Sets the volume of the player."""

        if not ctx.voice_state.is_playing:
            return await ctx.send('Nothing being played at the moment.')

        if 0 > volume > 100:
            return await ctx.send('Volume must be between 0 and 100')

        ctx.voice_state.volume = volume / 100
        await ctx.send('Volume of the player set to {}%'.format(volume))

    @commands.command(name='now', aliases=['current', 'playing'])
    async def _now(self, ctx: commands.Context):
        """Displays the currently playing song."""

        await ctx.send(embed=ctx.voice_state.current.create_embed())

    @commands.command(name='pause')
    @commands.has_permissions(manage_guild=True)
    async def _pause(self, ctx: commands.Context):
        """Pauses the currently playing song."""

        if ctx.voice_state.is_playing and ctx.voice_state.voice.is_playing():
            ctx.voice_state.voice.pause()
            await ctx.message.add_reaction('⏯')

    @commands.command(name='resume')
    @commands.has_permissions(manage_guild=True)
    async def _resume(self, ctx: commands.Context):
        """Resumes a currently paused song."""

        if ctx.voice_state.is_playing and ctx.voice_state.voice.is_paused():
            ctx.voice_state.voice.resume()
            await ctx.message.add_reaction('⏯')

    @commands.command(name='stop')
    @commands.has_permissions(manage_guild=True)
    async def _stop(self, ctx: commands.Context):
        """Stops playing song and clears the queue."""

        ctx.voice_state.songs.clear()

        if ctx.voice_state.is_playing:
            ctx.voice_state.voice.stop()
            await ctx.message.add_reaction('⏹')

    @commands.command(name='skip')
    async def _skip(self, ctx: commands.Context):
        """Vote to skip a song. The requester can automatically skip.
        3 skip votes are needed for the song to be skipped.
        """

        if not ctx.voice_state.is_playing:
            return await ctx.send('Not playing any music right now...')

        voter = ctx.message.author
        if voter == ctx.voice_state.current.requester:
            await ctx.message.add_reaction('⏭')
            ctx.voice_state.skip()

        elif voter.id not in ctx.voice_state.skip_votes:
            ctx.voice_state.skip_votes.add(voter.id)
            total_votes = len(ctx.voice_state.skip_votes)

            if total_votes >= 3:
                await ctx.message.add_reaction('⏭')
                ctx.voice_state.skip()
            else:
                await ctx.send('Skip vote added, currently at **{}/3**'.format(total_votes))

        else:
            await ctx.send('You have already voted to skip this song.')

    @commands.command(name='queue')
    async def _queue(self, ctx: commands.Context, *, page: int = 1):
        """Shows the player's queue.
        You can optionally specify the page to show. Each page contains 10 elements.
        """

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send('Empty queue.')

        items_per_page = 10
        pages = math.ceil(len(ctx.voice_state.songs) / items_per_page)

        start = (page - 1) * items_per_page
        end = start + items_per_page

        queue = ''
        for i, song in enumerate(ctx.voice_state.songs[start:end], start=start):
            queue += '`{0}.` [**{1.source.title}**]({1.source.url})\n'.format(i + 1, song)

        embed = (discord.Embed(description='**{} tracks:**\n\n{}'.format(len(ctx.voice_state.songs), queue))
                 .set_footer(text='Viewing page {}/{}'.format(page, pages)))
        await ctx.send(embed=embed)

    @commands.command(name='shuffle')
    async def _shuffle(self, ctx: commands.Context):
        """Shuffles the queue."""

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send('Empty queue.')

        ctx.voice_state.songs.shuffle()
        await ctx.message.add_reaction('✅')

    @commands.command(name='remove')
    async def _remove(self, ctx: commands.Context, index: int):
        """Removes a song from the queue at a given index."""

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send('Empty queue.')

        ctx.voice_state.songs.remove(index - 1)
        await ctx.message.add_reaction('✅')

    @commands.command(name='loop')
    async def _loop(self, ctx: commands.Context):
        """Loops the currently playing song.
        Invoke this command again to unloop the song.
        """

        if not ctx.voice_state.is_playing:
            return await ctx.send('Nothing being played at the moment.')

        # Inverse boolean value to loop and unloop.
        ctx.voice_state.loop = not ctx.voice_state.loop
        await ctx.message.add_reaction('✅')

    @commands.command(name='play')
    async def _play(self, ctx: commands.Context, *, search: str):
        """Plays a song.
        If there are songs in the queue, this will be queued until the
        other songs finished playing.
        This command automatically searches from various sites if no URL is provided.
        A list of these sites can be found here: https://rg3.github.io/youtube-dl/supportedsites.html
        """

        if not ctx.voice_state.voice:
            await ctx.invoke(self._join)

        async with ctx.typing():
            try:
                source = await YTDLSource.create_source(ctx, search, loop=self.bot.loop)
            except YTDLError as e:
                await ctx.send('An error occurred while processing this request: {}'.format(str(e)))
            else:
                song = Song(source)

                await ctx.voice_state.songs.put(song)
                await ctx.send('Enqueued {}'.format(str(source)))

    @_join.before_invoke
    @_play.before_invoke
    async def ensure_voice_state(self, ctx: commands.Context):
        if not ctx.author.voice or not ctx.author.voice.channel:
            raise commands.CommandError('You are not connected to any voice channel.')

        if ctx.voice_client:
            if ctx.voice_client.channel != ctx.author.voice.channel:
                raise commands.CommandError('Bot is already in a voice channel.')



bot = commands.Bot('music.', description='Yet another music bot.')
bot.add_cog(Music(bot))


@bot.event
async def on_ready():
    print('Logged in as:\n{0.user.name}\n{0.user.id}'.format(bot))

@bot.event
async def on_message(message):
    mention = f'<@!317000109680230400>'
    mention2 = f'<@317000109680230400>'
    
    messages_from_mee6 = ['Să mă iei de cuc {}',
                          'Mama ta știe că a fătat un ratat {}?',
                          'De ce sugi pula atata {}?',
                          'Bă {}. Nu mai fii poponar',
                          'Sugi pula {}',
                          'Știi ceva {}? Tu chiar mănânci sloboz cu câcat',
                          'Muie {}. Ia la muie. Muie muie muie',
                          'Să-ți fut familia {}',
                          '{} maimuţă electrocutată ce ești',
                          'Te bag în pizda mã-tii cu picioarele înainte ca sã-ți dau și muie dupã aia {}',
                          'Bă {}. Zii lui mã-ta sã nu își mai schimbe rujurile cã îmi face pula curcubeu',
                          '{}, eu nu am pulã ...... destulã pentru mã-ta',
                          'Să-mi usuc chiloții pe crucea mã-tii {}',
                          'Bãgami-aș pula în capul lui {} de imbecil avortat',
                          '{} Băgami-ai limba-n gaura curului să-mi gâdili hemoroizii',
                          '{} Dacă slobozul ar eroda, mă-ta ar fi la a 10-a proteză',
                          '{} Auzi mă pulă bleagă, o mai dor pe mă-ta genunchii ?',
                          '{} Când mă uit la fața ta, îmi aduc aminte de cea mai nespălată pulă pe care a supt-o mă-ta',
                          'Du-te dracu {} că dacă te scutur odată îți pică pulele din cur precum merele din pom',
                          '{} În dicționar, în dreptul cuvântului muie vezi poza lu mă-ta',
                          '{} Tu n-ai coaie, băi homosexual împuțit, tu ai o urmă de pulicică și două coșuri de le zici tu, mincinos mic, "coaie"',
                          'Nu ți-a ajuns câtă pulă ți-ai luat aseară la gingiile alea ca niște ciuperci stricate {}?',
                          'Ți-a mai zis cineva că puți a câcat cu miere, cu un strop de spermă și unul de untură de pește {} ?',
                          '{} Tu ești o grămadă de slobozi împrăștiați în atmosferă și redirectionați în gura lu mă-ta cu scopul de a-i crăcăna gaura curului care a fost înțepată de toți turcii care au cotropit România de-alungul anilor.',
                          'Ți-am spus {} de mii de ori că dacă nu te speli pe dinții ăia de raton paralizat nu te mai las să mă sugi de sloboz',
                          'Te mai duci la pescuit de pule {} ?',
                          'Sugia-ți-ar dracii pula s-o ducă în sahara iar tu sa rămai cu limba-n curu meu {}',
                          'Tu și cu mă-ta să vă luați bon de ordine ca să veniți să-mi sugeți pula {}. Nu de alta, dar înainte sunt toate rudele tale și toți morții mă-tii',
                          'Bă {}, cred ca tu ești un mare magician de ai reușit să sari din prezervativul lu tactu în pizda mă-tii, deserta-mi-aș coaiele în gâtu mă-tii!',
                          '{} Să te fut până ți s-or strepezi dinții, spaima pulii!',
                          'Stai la rând {} că nu sunt depozit de sloboz, o să opresc pentru tot neamu lu mă-ta, numai să vă săturați!',
                          '{} Fraieră a fost mă-ta când s-a crăcănat la tactu și te-a spircăit pe tine, amărâtule!',
                          'Proastă-i mă-ta la supt că suge de-o viață și tot trabant aveți {}',
                          'Bravo {}! Ești apreciat ca cel mai bun muist',
                          'baga-mi-aș pula peste mă-ta-n casă să-i mai fac un handicapat {}!',
                          '{} Să-mi bag pula peste mă-ta în casă să o dobor, mânca-mi-ai pula de la cotor de curvă lindicoasă',
                          'TUȘI-MI-AȘ CURUL ÎN GURA TA {} !',
                          'Dacă mă scol de pe mă-ta și îți înfig pula în carotidă, s-ar putea să ai nevoie de respirație pulă-n gură ca să îți revii {}',
                          '{} dependent de labă, să mori în budă când iți dă mă-ta aia proastă câcat cu lingurița',
                          'Să te vad mort și cu dinții rânjiți în pizda mă-tii {}',
                          '{} Să ți-o dea tactu la căcău până-ți ies ochii ca la melc',
                          '{} Te bagi și tu în seamă ca chiloții în curul lu curva de mă-ta',
                          'Uscami-aș prezervativele după ce le scot din zdreanța de mă-ta pe crucea lu tactu ăla labagiu {}',
                          'Să-mi bag pula în farmacistul de i-a dat prezervative găurite lu tactu de te-a făcut pe tine {}!',
                          'Da-ți-aș un pumn în pizda aia de gura ca să-ți sară pulile din cur {}',
                          'Ba {} accident biologic, handicapații nu au drept să vorbească pe serverul asta, așa că taci și suge',
                          'Tu sugi pula mai mult decât prevede codul de procedura penală {}',
                          'Mânca-mi-ai puroiul de la hemoroizii curului meu păros {} !',
                          'Vezi că iți pute gura a pulă de la 10 km {}',
                          'Dacă aș avea dinți în cur tu ai avea găuri în limbă {}',
                          'Bă {} tu du-te să faci labă la câcat până o să iasă pasta de dinți cu care o să te speli mândru pe gingii',
                          'Bă muie, să faci umbra pulii mele cu nasu {}',
                          '{} Dacă iți procesez un viol peste gingii o să vii la anul cu maxilarul încleștat de sloboz uscat.',
                          'Îți admir curajul {}, eu niciodată nu aș fi avut curajul să fiu în același timp și idiot și poponar',
                          '{} Să-mi bag capul pulii cu delicatețe în curul lu soră-ta, bordel de tenii',
                          '{} Să mi-o sugi ca și cum te-ai îneca și coaiele mele generoase ar fi pline cu oxigen',
                          'Băga-mi-aș pula în mă-ta că te-a fătat viu {}.',
                          'Să te bag în pizda mă-tii {}, dar nu de tot, numai cât să-ți rămână capul cu gura afara să mă cac în ea',
                          '{} Să-ți dea la muie tot poporul chinez și ca supliment să te fută în cur și indienii cu sulițele',
                          'Să facă mă-ta spume la pizdă ca mașina de spalat și apoi tu sa bei {}',
                          '{} Bășită e mă-ta aia că se cacă pe ea non stop și tu o ștergi la cur cu gura',
                          '{} Să-mi bag pula în gura mă-tii că nu rupe chitanțe pe facut muie',
                          '{} Aș fi putut fi tatăl tău, dar țiganul din fața mea a avut mărunt. Eu nu.',
                          '{} Mă-ta e ca un congelator: toată lumea își pune carnea în ea, vedea-o-aș chinuită în paturi de hotel!']
    
    if (str(message.author) == 'MEE6#4876'):
        if (str(message.author.nick) == 'Modaru Nivelaru'):
            await message.channel.send(random.choice(messages_from_mee6).format(message.author.mention))           
    if ((mention in message.content or mention2 in message.content) and str(message.author) == 'MEE6#0000'):
        await message.channel.send(random.choice(messages_from_mee6).format(message.author.mention))
    else:
        await bot.process_commands(message)

bot_token = os.getenv("token")
bot.run(bot_token)
