from functools import wraps
import random
import sys
import weakref

from numpy import fromstring, uint8

from twisted.internet import reactor
from twisted.internet.defer import Deferred, succeed
from twisted.internet.task import coiterate, deferLater, LoopingCall
from twisted.python import log

from bravo.chunk import Chunk
from bravo.compat import product
from bravo.config import configuration
from bravo.ibravo import ISerializerFactory
from bravo.plugin import retrieve_named_plugins
from bravo.utilities import split_coords

try:
    from ampoule import deferToAMPProcess
    from bravo.remote import MakeChunk
    async = configuration.getboolean("bravo", "ampoule")
except ImportError:
    async = False

def coords_to_chunk(f):
    """
    Automatically look up the chunk for the coordinates, and convert world
    coordinates to chunk coordinates.
    """

    @wraps(f)
    def decorated(self, coords, *args, **kwargs):
        x, y, z = coords

        bigx, smallx, bigz, smallz = split_coords(x, z)
        chunk = self.load_chunk(bigx, bigz)

        return f(self, chunk, (smallx, y, smallz), *args, **kwargs)

    return decorated

class World(object):
    """
    Object representing a world on disk.

    Worlds are composed of levels and chunks, each of which corresponds to
    exactly one file on disk. Worlds also contain saved player data.
    """

    season = None
    """
    The current `ISeason`.
    """

    saving = True
    """
    Whether objects belonging to this world may be written out to disk.
    """

    def __init__(self, name):
        """
        Load a world from disk.

        :Parameters:
            name : str
                The configuration key to use to look up configuration data.
        """

        world_url = configuration.get("world %s" % name, "url")
        world_sf_name = configuration.get("world %s" % name, "serializer")

        sf = retrieve_named_plugins(ISerializerFactory, [world_sf_name])[0]
        self.serializer = sf(world_url)

        self.chunk_cache = weakref.WeakValueDictionary()
        self.dirty_chunk_cache = dict()

        self._pending_chunks = dict()

        self.spawn = (0, 0, 0)
        self.seed = random.randint(0, sys.maxint)

        self.serializer.load_level(self)
        self.serializer.save_level(self)

        self.chunk_management_loop = LoopingCall(self.sort_chunks)
        self.chunk_management_loop.start(1)

        log.msg("World started on %s, using serializer %s" %
            (world_url, self.serializer.name))
        log.msg("Using Ampoule: %s" % async)

    def enable_cache(self, size):
        """
        Set the permanent cache size.

        Changing the size of the cache sets off a series of events which will
        empty or fill the cache to make it the proper size.

        For reference, 3 is a large-enough size to completely satisfy the
        Notchian client's login demands. 10 is enough to completely fill the
        Notchian client's chunk buffer.

        :param int size: The taxicab radius of the cache, in chunks
        """

        log.msg("Setting cache size to %d..." % size)

        self.permanent_cache = set()
        def assign(chunk):
            self.permanent_cache.add(chunk)

        rx = xrange(self.spawn[0] - size, self.spawn[0] + size)
        rz = xrange(self.spawn[2] - size, self.spawn[2] + size)
        d = coiterate(self.request_chunk(x, z).addCallback(assign)
            for x, z in product(rx, rz))
        d.addCallback(lambda chaff: log.msg("Cache size is now %d" % size))

    def sort_chunks(self):
        """
        Sort out the internal caches.

        This method will always block when there are dirty chunks.
        """

        first = True

        all_chunks = dict(self.dirty_chunk_cache)
        all_chunks.update(self.chunk_cache)
        self.chunk_cache.clear()
        self.dirty_chunk_cache.clear()
        for coords, chunk in all_chunks.iteritems():
            if chunk.dirty:
                if first:
                    first = False
                    self.save_chunk(chunk)
                    self.chunk_cache[coords] = chunk
                else:
                    self.dirty_chunk_cache[coords] = chunk
            else:
                self.chunk_cache[coords] = chunk

    def save_off(self):
        """
        Disable saving to disk.

        This is useful for accessing the world on disk without Bravo
        interfering, for backing up the world.
        """

        if not self.saving:
            return

        d = dict(self.chunk_cache)
        self.chunk_cache = d
        self.saving = False

    def save_on(self):
        """
        Enable saving to disk.
        """

        if self.saving:
            return

        d = weakref.WeakValueDictionary(self.chunk_cache)
        self.chunk_cache = d
        self.saving = True

    def populate_chunk(self, chunk):
        """
        Recreate data for a chunk.

        This method does arbitrary terrain generation depending on the current
        plugins, and then regenerates the chunk metadata so that the chunk can
        be sent to clients.

        A lot of maths may be done by this method, so do not call it unless
        absolutely necessary, e.g. when the chunk is created for the first
        time.
        """

        for stage in self.pipeline:
            stage.populate(chunk, self.seed)

        chunk.regenerate()

    def request_chunk(self, x, z):
        """
        Request a ``Chunk`` to be delivered later.

        :returns: Deferred that will be called with the Chunk
        """

        if not async:
            return deferLater(reactor, 0.000001, self.factory.world.load_chunk,
                x, z)

        if (x, z) in self.chunk_cache:
            return succeed(self.chunk_cache[x, z])
        elif (x, z) in self.dirty_chunk_cache:
            return succeed(self.dirty_chunk_cache[x, z])
        elif (x, z) in self._pending_chunks:
            # Rig up another Deferred and wrap it up in a to-go box.
            d = Deferred()
            self._pending_chunks[x, z].chainDeferred(d)
            return d

        chunk = Chunk(x, z)
        self.serializer.load_chunk(chunk)

        if chunk.populated:
            self.chunk_cache[x, z] = chunk
            return succeed(chunk)

        d = deferToAMPProcess(MakeChunk, x=x, z=z, seed=self.seed,
            generators=configuration.getlist("bravo", "generators"))
        self._pending_chunks[x, z] = d

        def pp(kwargs):
            chunk.blocks = fromstring(kwargs["blocks"],
                dtype=uint8).reshape(chunk.blocks.shape)
            chunk.heightmap = fromstring(kwargs["heightmap"],
                dtype=uint8).reshape(chunk.heightmap.shape)
            chunk.metadata = fromstring(kwargs["metadata"],
                dtype=uint8).reshape(chunk.metadata.shape)
            chunk.skylight = fromstring(kwargs["skylight"],
                dtype=uint8).reshape(chunk.skylight.shape)
            chunk.blocklight = fromstring(kwargs["blocklight"],
                dtype=uint8).reshape(chunk.blocklight.shape)

            chunk.populated = True
            chunk.dirty = True

            # Apply the current season to the chunk.
            if self.season:
                self.season.transform(chunk)

            # Since this chunk hasn't been given to any player yet, there's no
            # conceivable way that any meaningful damage has been accumulated;
            # anybody loading any part of this chunk will want the entire thing.
            # Thus, it should start out undamaged.
            chunk.clear_damage()

            self.dirty_chunk_cache[x, z] = chunk
            del self._pending_chunks[x, z]

            return chunk

        # Set up callbacks.
        d.addCallback(pp)
        # Multiple people might be subscribed to this pending callback. We're
        # going to keep it for ourselves and fork off another Deferred for our
        # caller.
        forked = Deferred()
        d.chainDeferred(forked)
        return forked

    def load_chunk(self, x, z):
        """
        Retrieve a ``Chunk`` synchronously.

        This method does lots of automatic caching of chunks to ensure that
        disk I/O is kept to a minimum.
        """

        if (x, z) in self.chunk_cache:
            return self.chunk_cache[x, z]
        elif (x, z) in self.dirty_chunk_cache:
            return self.dirty_chunk_cache[x, z]

        chunk = Chunk(x, z)
        self.serializer.load_chunk(chunk)

        if chunk.populated:
            self.chunk_cache[x, z] = chunk
        else:
            self.populate_chunk(chunk)
            chunk.populated = True
            chunk.dirty = True

            self.dirty_chunk_cache[x, z] = chunk

        # Apply the current season to the chunk.
        if self.season:
            self.season.transform(chunk)

        # Since this chunk hasn't been given to any player yet, there's no
        # conceivable way that any meaningful damage has been accumulated;
        # anybody loading any part of this chunk will want the entire thing.
        # Thus, it should start out undamaged.
        chunk.clear_damage()

        return chunk

    def save_chunk(self, chunk):

        if not chunk.dirty:
            return

        self.serializer.save_chunk(chunk)

        chunk.dirty = False

    def load_player(self, username):
        """
        Retrieve player data.
        """

        player = self.factory.create_entity(self.spawn[0], self.spawn[1],
            self.spawn[2], "Player", username=username)

        player.location.stance = self.spawn[1]
        player.username = username

        self.serializer.load_player(player)

        return player

    def save_player(self, username, player):
        self.serializer.save_player(player)

    # World-level geometry access.
    # These methods let external API users refrain from going through the
    # standard motions of looking up and loading chunk information.

    @coords_to_chunk
    def get_block(self, chunk, coords):
        """
        Get a block from an unknown chunk.
        """

        return chunk.get_block(coords)

    @coords_to_chunk
    def set_block(self, chunk, coords, value):
        """
        Set a block in an unknown chunk.
        """

        chunk.set_block(coords, value)

    @coords_to_chunk
    def get_metadata(self, chunk, coords):
        """
        Get a block's metadata from an unknown chunk.
        """

        return chunk.get_metadata(coords)

    @coords_to_chunk
    def set_metadata(self, chunk, coords, value):
        """
        Set a block's metadata in an unknown chunk.
        """

        chunk.set_metadata(coords, value)

    @coords_to_chunk
    def destroy(self, chunk, coords):
        """
        Destroy a block in an unknown chunk.
        """

        chunk.destroy(coords)
