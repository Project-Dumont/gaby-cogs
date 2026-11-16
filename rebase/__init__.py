from .rebase import Rebase

async def setup(bot):
    await bot.add_cog(Rebase(bot))
