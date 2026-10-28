package multicraft.host;

/** Pure deterministic scheduler check; no Minecraft, clocks, windows or native calls. */
public final class FrameScheduleCheck {
    private static int frames(int fps,long loopStep,long duration) {
        long deadline=0; int frames=0;
        for(long now=1;now<duration;now+=loopStep) if(now>=deadline) {
            frames++; deadline=FrameSchedule.nextDeadline(deadline,now,fps);
            if(deadline<=now) throw new AssertionError("Frame debt was retained");
        }
        return frames;
    }
    public static void main(String[] args) {
        int own=frames(30,22_000_000L,10_000_000_000L),peer=frames(60,22_000_000L,10_000_000_000L);
        if(own<295 || own>305 || peer<450 || peer>460)
            throw new AssertionError("22ms peer loop quantized the 30fps slot: "+own+"/"+peer);
        long period=1_000_000_000L/30,late=2_500_000_000L;
        long afterStall=FrameSchedule.nextDeadline(period,late,30);
        if(afterStall<=late || afterStall>late+period) throw new AssertionError("Unbounded stall debt");
        if(FrameSchedule.nextDeadline(0,late,10)!=late+100_000_000L) throw new AssertionError("Changed cap did not reset phase");
        int low=frames(10,22_000_000L,10_000_000_000L);
        if(low<99 || low>101) throw new AssertionError("Low-rate slot drifted: "+low);
        try { FrameSchedule.nextDeadline(0,1,0); throw new AssertionError("Invalid rate accepted"); }
        catch(IllegalArgumentException expected) {}
        System.out.println("PURE SCHEDULER CHECK: 22ms owner loop yields "+own+" frames at target30, peer "+peer
            +" frames at target60 over10s; long stall drops expired budgets; no native FPS claim.");
    }
}
