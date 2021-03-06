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
            await ctx.message.add_reaction('???')

    @commands.command(name='resume')
    @commands.has_permissions(manage_guild=True)
    async def _resume(self, ctx: commands.Context):
        """Resumes a currently paused song."""

        if ctx.voice_state.is_playing and ctx.voice_state.voice.is_paused():
            ctx.voice_state.voice.resume()
            await ctx.message.add_reaction('???')

    @commands.command(name='stop')
    @commands.has_permissions(manage_guild=True)
    async def _stop(self, ctx: commands.Context):
        """Stops playing song and clears the queue."""

        ctx.voice_state.songs.clear()

        if ctx.voice_state.is_playing:
            ctx.voice_state.voice.stop()
            await ctx.message.add_reaction('???')

    @commands.command(name='skip')
    async def _skip(self, ctx: commands.Context):
        """Vote to skip a song. The requester can automatically skip.
        3 skip votes are needed for the song to be skipped.
        """

        if not ctx.voice_state.is_playing:
            return await ctx.send('Not playing any music right now...')

        voter = ctx.message.author
        if voter == ctx.voice_state.current.requester:
            await ctx.message.add_reaction('???')
            ctx.voice_state.skip()

        elif voter.id not in ctx.voice_state.skip_votes:
            ctx.voice_state.skip_votes.add(voter.id)
            total_votes = len(ctx.voice_state.skip_votes)

            if total_votes >= 3:
                await ctx.message.add_reaction('???')
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
        await ctx.message.add_reaction('???')

    @commands.command(name='remove')
    async def _remove(self, ctx: commands.Context, index: int):
        """Removes a song from the queue at a given index."""

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send('Empty queue.')

        ctx.voice_state.songs.remove(index - 1)
        await ctx.message.add_reaction('???')

    @commands.command(name='loop')
    async def _loop(self, ctx: commands.Context):
        """Loops the currently playing song.
        Invoke this command again to unloop the song.
        """

        if not ctx.voice_state.is_playing:
            return await ctx.send('Nothing being played at the moment.')

        # Inverse boolean value to loop and unloop.
        ctx.voice_state.loop = not ctx.voice_state.loop
        await ctx.message.add_reaction('???')

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
    
    messages_from_mee6 = ['S?? m?? iei de cuc {}',
                          'Mama ta ??tie c?? a f??tat un ratat {}?',
                          'De ce sugi pula atata {}?',
                          'B?? {}. Nu mai fii poponar',
                          'Sugi pula {}',
                          '??tii ceva {}? Tu chiar m??n??nci sloboz cu c??cat',
                          'Muie {}. Ia la muie. Muie muie muie',
                          'S??-??i fut familia {}',
                          '{} maimu???? electrocutat?? ce e??ti',
                          'Te bag ??n pizda m??-tii cu picioarele ??nainte ca s??-??i dau ??i muie dup?? aia {}',
                          'B?? {}. Zii lui m??-ta s?? nu ????i mai schimbe rujurile c?? ??mi face pula curcubeu',
                          '{}, eu nu am pul?? ...... destul?? pentru m??-ta',
                          'S??-mi usuc chilo??ii pe crucea m??-tii {}',
                          'B??gami-a?? pula ??n capul lui {} de imbecil avortat',
                          '{} B??gami-ai limba-n gaura curului s??-mi g??dili hemoroizii',
                          '{} Dac?? slobozul ar eroda, m??-ta ar fi la a 10-a protez??',
                          '{} Auzi m?? pul?? bleag??, o mai dor pe m??-ta genunchii ?',
                          '{} C??nd m?? uit la fa??a ta, ??mi aduc aminte de cea mai nesp??lat?? pul?? pe care a supt-o m??-ta',
                          'Du-te dracu {} c?? dac?? te scutur odat?? ????i pic?? pulele din cur precum merele din pom',
                          '{} ??n dic??ionar, ??n dreptul cuv??ntului muie vezi poza lu m??-ta',
                          '{} Tu n-ai coaie, b??i homosexual ??mpu??it, tu ai o urm?? de pulicic?? ??i dou?? co??uri de le zici tu, mincinos mic, "coaie"',
                          'Nu ??i-a ajuns c??t?? pul?? ??i-ai luat asear?? la gingiile alea ca ni??te ciuperci stricate {}?',
                          '??i-a mai zis cineva c?? pu??i a c??cat cu miere, cu un strop de sperm?? ??i unul de untur?? de pe??te {} ?',
                          '{} Tu e??ti o gr??mad?? de slobozi ??mpr????tia??i ??n atmosfer?? ??i redirectiona??i ??n gura lu m??-ta cu scopul de a-i cr??c??na gaura curului care a fost ??n??epat?? de to??i turcii care au cotropit Rom??nia de-alungul anilor.',
                          '??i-am spus {} de mii de ori c?? dac?? nu te speli pe din??ii ??ia de raton paralizat nu te mai las s?? m?? sugi de sloboz',
                          'Te mai duci la pescuit de pule {} ?',
                          'Sugia-??i-ar dracii pula s-o duc?? ??n sahara iar tu sa r??mai cu limba-n curu meu {}',
                          'Tu ??i cu m??-ta s?? v?? lua??i bon de ordine ca s?? veni??i s??-mi suge??i pula {}. Nu de alta, dar ??nainte sunt toate rudele tale ??i to??i mor??ii m??-tii',
                          'B?? {}, cred ca tu e??ti un mare magician de ai reu??it s?? sari din prezervativul lu tactu ??n pizda m??-tii, deserta-mi-a?? coaiele ??n g??tu m??-tii!',
                          '{} S?? te fut p??n?? ??i s-or strepezi din??ii, spaima pulii!',
                          'Stai la r??nd {} c?? nu sunt depozit de sloboz, o s?? opresc pentru tot neamu lu m??-ta, numai s?? v?? s??tura??i!',
                          '{} Fraier?? a fost m??-ta c??nd s-a cr??c??nat la tactu ??i te-a spirc??it pe tine, am??r??tule!',
                          'Proast??-i m??-ta la supt c?? suge de-o via???? ??i tot trabant ave??i {}',
                          'Bravo {}! E??ti apreciat ca cel mai bun muist',
                          'baga-mi-a?? pula peste m??-ta-n cas?? s??-i mai fac un handicapat {}!',
                          '{} S??-mi bag pula peste m??-ta ??n cas?? s?? o dobor, m??nca-mi-ai pula de la cotor de curv?? lindicoas??',
                          'TU??I-MI-A?? CURUL ??N GURA TA {} !',
                          'Dac?? m?? scol de pe m??-ta ??i ????i ??nfig pula ??n carotid??, s-ar putea s?? ai nevoie de respira??ie pul??-n gur?? ca s?? ????i revii {}',
                          '{} dependent de lab??, s?? mori ??n bud?? c??nd i??i d?? m??-ta aia proast?? c??cat cu linguri??a',
                          'S?? te vad mort ??i cu din??ii r??nji??i ??n pizda m??-tii {}',
                          '{} S?? ??i-o dea tactu la c??c??u p??n??-??i ies ochii ca la melc',
                          '{} Te bagi ??i tu ??n seam?? ca chilo??ii ??n curul lu curva de m??-ta',
                          'Uscami-a?? prezervativele dup?? ce le scot din zdrean??a de m??-ta pe crucea lu tactu ??la labagiu {}',
                          'S??-mi bag pula ??n farmacistul de i-a dat prezervative g??urite lu tactu de te-a f??cut pe tine {}!',
                          'Da-??i-a?? un pumn ??n pizda aia de gura ca s??-??i sar?? pulile din cur {}',
                          'Ba {} accident biologic, handicapa??ii nu au drept s?? vorbeasc?? pe serverul asta, a??a c?? taci ??i suge',
                          'Tu sugi pula mai mult dec??t prevede codul de procedura penal?? {}',
                          'M??nca-mi-ai puroiul de la hemoroizii curului meu p??ros {} !',
                          'Vezi c?? i??i pute gura a pul?? de la 10 km {}',
                          'Dac?? a?? avea din??i ??n cur tu ai avea g??uri ??n limb?? {}',
                          'B?? {} tu du-te s?? faci lab?? la c??cat p??n?? o s?? ias?? pasta de din??i cu care o s?? te speli m??ndru pe gingii',
                          'B?? muie, s?? faci umbra pulii mele cu nasu {}',
                          '{} Dac?? i??i procesez un viol peste gingii o s?? vii la anul cu maxilarul ??ncle??tat de sloboz uscat.',
                          '????i admir curajul {}, eu niciodat?? nu a?? fi avut curajul s?? fiu ??n acela??i timp ??i idiot ??i poponar',
                          '{} S??-mi bag capul pulii cu delicate??e ??n curul lu sor??-ta, bordel de tenii',
                          '{} S?? mi-o sugi ca ??i cum te-ai ??neca ??i coaiele mele generoase ar fi pline cu oxigen',
                          'B??ga-mi-a?? pula ??n m??-ta c?? te-a f??tat viu {}.',
                          'S?? te bag ??n pizda m??-tii {}, dar nu de tot, numai c??t s??-??i r??m??n?? capul cu gura afara s?? m?? cac ??n ea',
                          '{} S??-??i dea la muie tot poporul chinez ??i ca supliment s?? te fut?? ??n cur ??i indienii cu suli??ele',
                          'S?? fac?? m??-ta spume la pizd?? ca ma??ina de spalat ??i apoi tu sa bei {}',
                          '{} B????it?? e m??-ta aia c?? se cac?? pe ea non stop ??i tu o ??tergi la cur cu gura',
                          '{} S??-mi bag pula ??n gura m??-tii c?? nu rupe chitan??e pe facut muie',
                          '{} A?? fi putut fi tat??l t??u, dar ??iganul din fa??a mea a avut m??runt. Eu nu.',
                          '{} M??-ta e ca un congelator: toat?? lumea ????i pune carnea ??n ea, vedea-o-a?? chinuit?? ??n paturi de hotel!',
                          'Sugruma-mi-ai ??tromeleagul cu corzile tale vocale {}',
                          '??neca-mi-ai pula cu saliva m??-tii {}',
                          '{} Trage aer ??n cur c?? nu o s?? mai po??i respira de at??ta pul?? c??t?? o s?? prime??ti, sugea-mi-ai nectaru din pula.',
                          'C??ca-m-a?? pe morm??ntu t??u s?? aibe m??-ta ??n ce s?? ??nfig?? lumanarea {}.',
                          '{} Tu s??-mi sufli ??n pul?? p??n?? o s?? fac aburi s?? spui c?? te-am futut ??n stil trenuletz de epoca',
                          '{} Dac?? vrei coaie, hai la tata s?? ??i le dea pe la buzi??oare de n-ai s?? mai po??i zice nici cum te cheam??, b??i rahat cu girofar ce e??ti',
                          'Esti o pu??ulic?? de sconcs pansat {}',
                          '{} Am auzit ca m??-ta se duce noaptea c??nd tu dormi, vinde un kil de pizd??, ia 500 g de pul?? ??i i??i d?? diminea??a, sub form?? de c??rna??i s?? m??n??nci',
                          'Dac?? ai avea capu de fier, ar rugini de c??t?? muie ai luat {}. Am impresia c?? ai ramas ??nc?? ??n stadiul de spermatozoid',
                          '{} E nes????ioas?? r??u m??-ta, pot sa cred ca e ramp?? de lansare pentru putori ',
                          '{} Zi-i lu m??-ta c?? mai are mult de supt ca s??-??i pl??teasc?? taxa de prost, at??ta-i de mare',
                          'Detona-mi-a??i pula ??ntre m??selele tale {}',
                          'S?? ai parte de fela??ie de la toate babele peste 81 de ani {}',
                          '{} ????i fac cuno??tin???? cu domnu` Capu` Pulii p??n??-n inima aia a ta de muist l??bar',
                          'Acolo la ??coala mea era m??-ta educatoare {}. Ne educa pulile ca s?? se comporte bine ??n gura ei',
                          'E??ti de o prostie rar?? {}, r??m??n urme pe asfalt pe unde calci',
                          'Dac?? ar durea prostia cred c?? tu ai fi tot timpul in com?? {}',
                          'S?? rozi fiecare spermatozoid ??ntre din??ii t??i caria??i de sperm?? {}.',
                          '{} Dac?? a?? fi avut la momentul potrivit 10 de lei, acum ??i-a?? fi fost tat??',
                          'S??-??i ??ndop pula-n cur p??n?? faci ocluzie intestinal?? {}',
                          '{} Limbajul t??u denot?? tulburari hormonale de virgin cu co??uri pe fa????',
                          '{} Aurolacu pulii mele, ia pieli??a pulii, respir?? ??n ea ??i o s?? ai un kinder cu surprize',
                          '{} Mai ai ceva de zis s?? i??i dau un cur de lins ',
                          'Lua-mi-ai c??catul la polizor s??-??i sar?? a??chii ??n gur?? {}',
                          'S??-mi bag coaiele ??n gura lu m??-ta {}. La tine n-am curaj c?? mi le ??nghi??i',
                          '{} S?? m?? plimb cu trenu unde m??-ta e pe post de taxator cu pizd?? la urcarea ??n vagoane']
    
    if (str(message.author) == 'MEE6#4876'):
        if (str(message.author.nick) == 'Modaru Nivelaru'):
            await message.channel.send(random.choice(messages_from_mee6).format(message.author.mention))           
    if ((mention in message.content or mention2 in message.content) and str(message.author) == 'MEE6#0000'):
        await message.channel.send(random.choice(messages_from_mee6).format(message.author.mention))
    else:
        await bot.process_commands(message)
    if (str(message.author) == 'OmuRoshuCuUnBatz#8792' and 'muie.popa' in message.content):
        await message.channel.send(random.choice(messages_from_mee6).format('<@!318429439690276864>'))

bot_token = os.getenv("token")
bot.run(bot_token)
#dummy commit
