# MultiCraft

MultiCraft is a Minecraft client multi-instance launcher and browser streamer.
- Keep ordinary native client/server connections unchanged.
- One actual client runtime JVM may hold multiple independent protocol, world, player, GUI, input and audio sessions. Do not claim GPU geometry sharing or energy savings without direct evidence.
- The product UI uses the established single-player M0 layout, one identity per browser tab. Pointer capture and browser fullscreen are separate actions.
- Keep identity and input/media ownership isolated, including browser tabs and reconnect epochs. Only the explicitly configured operator may manage profiles and limits. The current deployment operator remains asta.
- Pin Minecraft, JDK, native libraries and media versions. Publish only owned source/assets and dependency manifests, never game JARs/assets/worlds, credentials, public deployment details, or runtime state.
- Use dedicated isolated test directories, world and ports. Never restart or change the existing M0 service, accepted M1 service, production Minecraft, or observation prototype during release testing.
- No package installation without requesting it from the user first. Existing installed tools/libraries and read-only verified game caches may be reused.
- All code changes require an independent read-only review. Distinguish static tests, real native gameplay, actual media decoding, and user-confirmed audio.
