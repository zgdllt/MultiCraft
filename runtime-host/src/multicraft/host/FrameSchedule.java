package multicraft.host;

/** Deadline phase survives peer work; missed frame budgets are discarded, never queued. */
public final class FrameSchedule {
    private FrameSchedule() {}
    public static long nextDeadline(long previousDeadline,long frameStart,int fps) {
        if(fps<1 || fps>1000) throw new IllegalArgumentException("Frame limit requires 1..1000");
        long period=1_000_000_000L/fps;
        if(previousDeadline==0) return frameStart+period;
        long next=previousDeadline+period;
        if(next<=frameStart) next+=((frameStart-next)/period+1)*period;
        return next;
    }
}
