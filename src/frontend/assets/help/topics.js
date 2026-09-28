// Spec 19 help topics. Each clip is a short, silent, looping capture of the
// real app (assets/help/<clip>.mp4 with a <clip>.webp still). The text says
// everything the clip shows, so the clip is never required to understand it.
export const HELP_TOPICS = {
    building: {
        title: 'Building',
        clip: 'building',
        purpose: 'Shape the building the earthquake will shake.',
        steps: [
            'Drag Stories and Floor area; the building updates after you let go.',
            'Open Structure for mass, damping and member sizes.',
            'Open Irregularities to add a soft ground story or a weak story.',
        ],
        technical: [
            'The period T₁ is an output: it follows from mass and from the column and beam sizes, which set the stiffness.',
            'A soft ground story and a weak story change the building itself, so they affect both elastic and collapse runs.',
        ],
    },
    'code-design': {
        title: 'Code design',
        clip: 'code-design',
        purpose: 'Generate member sizes from seismic-code steps, review the preview, then apply it.',
        steps: [
            'Choose Code design and set stories, story height, area and the code classes.',
            'Read the preview: it is marked Not applied until you press Apply building.',
            'Use Details for the per-story member table; Compare runs it against a soft-story version.',
        ],
        technical: [
            'This is an educational approximation, not a verified BNBC or ACI design. Every code constant is marked TO VERIFY.',
            'Each story is sized separately, so column depths taper with height; Details lists them all.',
        ],
    },
    earthquake: {
        title: 'Earthquake',
        clip: 'earthquake',
        purpose: 'Start from a real recording, then explore an adjusted scenario.',
        steps: [
            'Pick a record in the header; the i button beside it describes the event.',
            'Move Magnitude, Epicenter distance or Depth to rescale the recording.',
            'The badge changes to Modified scenario; Reset returns to the recording.',
        ],
        technical: [
            'Magnitude and distance scale the whole recording by one factor, so the shape of the shaking does not change; the printed peaks do.',
            'The historical event facts never change when you adjust the scenario.',
        ],
    },
    analysis: {
        title: 'Collapse analysis',
        clip: 'analysis',
        purpose: 'Run a nonlinear analysis to see yielding and, if it happens, a story detaching.',
        steps: [
            'Load collapse demo sets a known detaching case, or keep your own building.',
            'Press Run collapse analysis and wait; the elapsed time is shown.',
            'Read the result card: onset and detachment are computed; the fall after it is illustrative.',
        ],
        technical: [
            'Analysis intensity multiplies the ground motion for the nonlinear run only. It is not the event magnitude.',
            'A run that does not converge reports no collapse; the previous result stays on screen.',
        ],
    },
    view: {
        title: 'View',
        clip: 'view',
        purpose: 'Inspect one floor or the whole building, and choose how motion is shown.',
        steps: [
            'Pick a floor in Camera focus to zoom to it; Back to full view returns.',
            'Drag in the scene to orbit and scroll or pinch to zoom.',
            'Motion settings control camera shake, the cinematic camera and slow motion.',
        ],
        technical: [
            'Camera focus only moves the camera. The chart floor is chosen separately in Signals.',
        ],
    },
    signals: {
        title: 'Signals',
        clip: 'signals',
        purpose: 'Inspect the same response in time and in frequency.',
        steps: [
            'Choose a Plot floor and an axis; every chart uses them.',
            'Time shows ground acceleration in and floor motion out as the playhead moves.',
            'Experiments holds superposition, the intensity sweep and the building comparison.',
        ],
    },
    time: {
        title: 'Time chart',
        clip: 'signals',
        purpose: 'Ground acceleration in, floor displacement out, over the whole record.',
        steps: [
            'The top trace is the actual scaled ground acceleration.',
            'The middle trace is the plot floor’s displacement relative to the ground.',
            'The bottom trace is the nonlinear correction force; it is zero in an elastic run.',
        ],
        technical: [
            'Each panel prints its own peak, because a magnitude change scales the curve and its self-normalised axis together.',
        ],
    },
    frequency: {
        title: 'Frequency chart',
        clip: 'signals',
        purpose: 'The same motion as a spectrum: input, the building’s transfer function, and the measured output.',
        steps: [
            'Input is the ground-motion spectrum; peaks in Transfer are the building’s natural frequencies.',
            'Output is the FFT of the actual floor motion, not Input × Transfer.',
            'After a collapse run, the identity section compares elastic, corrected and measured spectra.',
        ],
        technical: [
            'Output is measured from the relative floor displacement, so the identity Output = Transfer × Input is a check, not an assumption.',
        ],
    },
    hysteresis: {
        title: 'Story hysteresis',
        clip: 'hysteresis',
        purpose: 'Story force versus drift through loading and unloading.',
        steps: [
            'Run a collapse analysis first; elastic runs have no loops.',
            'Choose a Plot story and axis; the trail follows the playhead.',
            'The printed work is ∫V dδ so far, including energy still stored elastically.',
        ],
    },
    convergence: {
        title: 'Convergence',
        clip: 'convergence',
        purpose: 'Check whether the nonlinear calculation converged.',
        steps: [
            'Each dot is one iteration’s residual; the dashed line is the tolerance.',
            'Converged means the residual fell below the tolerance.',
            'Did not converge never means the building collapsed.',
        ],
        technical: [
            'The nonlinear response is solved as a linear FFT system driven by a pseudo-force, found by fixed-point iteration (a contraction mapping when it converges).',
        ],
    },
};
