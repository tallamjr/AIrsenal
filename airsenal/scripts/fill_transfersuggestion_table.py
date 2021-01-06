#!/usr/bin/env python

"""
usage:
python fill_transfersuggestsions_table.py --weeks_ahead <num_weeks_ahead>
                                          --num_iterations <num_iterations>
output for each strategy tried is going to be a dict
{ "total_points": <float>,
"points_per_gw": {<gw>: <float>, ...},
"players_sold" : {<gw>: [], ...},
"players_bought" : {<gw>: [], ...}
}
This is done via a recursive tree search, where nodes on the tree do an optimization
for a given number of transfers, then adds some children to the multiprocessing queue
representing 0, 1, 2 transfers for the next gameweek.

"""

import os
import shutil
import time
import json
import sys

import cProfile


from multiprocessing import Process
from tqdm import tqdm
import argparse

from airsenal.framework.multiprocessing_utils import CustomQueue
from airsenal.framework.optimization_utils import (
    get_starting_squad,
    calc_free_transfers,
    calc_points_hit,
    fill_suggestion_table,
    make_best_transfers,
    get_num_increments,
    count_expected_outputs,
    next_week_transfers,
    check_tag_valid,
)

from airsenal.framework.utils import (
    CURRENT_SEASON,
    get_player_name,
    get_latest_prediction_tag,
    get_next_gameweek,
    get_free_transfers,
    fetcher,
)

if os.name == "posix":
    TMPDIR = "/tmp/"
else:
    TMPDIR = "%TMP%"

OUTPUT_DIR = os.path.join(TMPDIR, "airsopt")


def is_finished(final_expected_num):
    """
    Count the number of json files in the output directory, and see if the number
    matches the final expected number, which should be pre-calculated by the
    count_expected_points function based on the number of weeks optimising for, chips
    available and other constraints.
    Return True if output files are all there, False otherwise.
    """

    # count the json files in the output dir
    json_count = len(os.listdir(OUTPUT_DIR))
    if json_count == final_expected_num:
        return True
    return False


def optimize(
    queue,
    pid,
    num_expected_outputs,
    gameweek_range,
    season,
    pred_tag,
    chips_gw_dict,
    max_total_hit=None,
    allow_unused_transfers=True,
    max_transfers=2,
    num_iterations=100,
    updater=None,
    resetter=None,
    profile=False,
):
    """
    Queue is the multiprocessing queue,
    pid is the Process that will execute this func,
    gameweeks will be a list of gameweeks to consider,
    season and prediction_tag are hopefully self-explanatory.

    The rest of the parameters needed for prediction are from the queue.

    Things on the queue will either be "FINISHED", or a tuple:
    (
     num_transfers,
     free_transfers,
     hit_so_far,
     current_team,
     strat_dict,
     strat_id
    )
    """
    while True:
        if queue.qsize() > 0:
            status = queue.get()
        else:
            if is_finished(num_expected_outputs):
                break
            else:
                time.sleep(5)
                continue

        # now assume we have set of parameters to do an optimization
        # from the queue.

        # turn on the profiler if requested
        if profile:
            profiler = cProfile.Profile()
            profiler.enable()

        num_transfers, free_transfers, hit_so_far, squad, strat_dict, sid = status
        # num_transfers will be 0, 1, 2, OR 'W' or 'F', OR 'T0', T1', 'T2',
        # OR 'B0', 'B1', or 'B2' (the latter six represent triple captain or
        # bench boost along with 0, 1, or 2 transfers).

        # sid (status id) is just a string e.g. "0-0-2" representing how many
        # transfers to be made in each gameweek.
        # Only exception is the root node, where sid is "starting" - this
        # node only exists to add children to the queue.

        if sid == "starting":
            sid = ""
            depth = 0
            strat_dict["total_score"] = 0
            strat_dict["points_per_gw"] = {}
            strat_dict["players_in"] = {}
            strat_dict["players_out"] = {}
            strat_dict["chips_played"] = {}
            new_squad = squad
            gw = gameweek_range[0] - 1
        else:
            if len(sid) > 0:
                sid += "-"
            sid += str(num_transfers)
            resetter(pid, sid)

            # work out what gameweek we're in and how far down the tree we are.
            depth = len(strat_dict["points_per_gw"])

            # gameweeks from this point in strategy to end of window
            gameweeks = gameweek_range[depth:]

            # upcoming gameweek:
            gw = gameweeks[0]

            # check whether we're playing a chip this gameweek
            if isinstance(num_transfers, str):
                if num_transfers.startswith("T"):
                    strat_dict["chips_played"][gw] = "triple_captain"
                elif num_transfers.startswith("B"):
                    strat_dict["chips_played"][gw] = "bench_boost"
                elif num_transfers == "W":
                    strat_dict["chips_played"][gw] = "wildcard"
                elif num_transfers == "F":
                    strat_dict["chips_played"][gw] = "free_hit"
            else:
                strat_dict["chips_played"][gw] = None

            # calculate best transfers to make this gameweek (to maximise points across
            # remaining gameweeks)
            num_increments_for_updater = get_num_increments(
                num_transfers, num_iterations
            )
            increment = 100 / num_increments_for_updater
            new_squad, transfers, points = make_best_transfers(
                num_transfers,
                squad,
                pred_tag,
                gameweeks,
                season,
                num_iterations,
                (updater, increment, pid),
            )

            points -= calc_points_hit(num_transfers, free_transfers)
            strat_dict["total_score"] += points
            strat_dict["points_per_gw"][gw] = points

            strat_dict["players_in"][gw] = transfers["in"]
            strat_dict["players_out"][gw] = transfers["out"]
            free_transfers = calc_free_transfers(num_transfers, free_transfers)

            depth += 1

        if depth >= len(gameweek_range):
            with open(
                os.path.join(OUTPUT_DIR, "strategy_{}_{}.json".format(pred_tag, sid)),
                "w",
            ) as outfile:
                json.dump(strat_dict, outfile)
            # call function to update the main progress bar
            updater()

            if profile:
                profiler.dump_stats(f"process_strat_{pred_tag}_{sid}.pstat")

        else:
            # add children to the queue
            strategies = next_week_transfers(
                (free_transfers, hit_so_far, strat_dict),
                max_total_hit=max_total_hit,
                allow_unused_transfers=allow_unused_transfers,
                max_transfers=max_transfers,
                chips=chips_gw_dict[gw + 1],
            )

            for strat in strategies:
                # strat: (num_transfers, free_transfers, hit_so_far)
                num_transfers, free_transfers, hit_so_far = strat

                queue.put(
                    (
                        num_transfers,
                        free_transfers,
                        hit_so_far,
                        new_squad,
                        strat_dict,
                        sid,
                    )
                )


def find_best_strat_from_json(tag):
    """
    Look through all the files in our tmp directory that
    contain the prediction tag in their filename.
    Load the json, and find the strategy with the best 'total_score'.
    """
    best_score = 0
    best_strat = None
    file_list = os.listdir(OUTPUT_DIR)
    for filename in file_list:
        if not "strategy_{}_".format(tag) in filename:
            continue
        full_filename = os.path.join(OUTPUT_DIR, filename)
        with open(full_filename) as strat_file:
            strat = json.load(strat_file)
            if strat["total_score"] > best_score:
                best_score = strat["total_score"]
                best_strat = strat

    return best_strat


def save_baseline_score(squad, gameweeks, tag, season=CURRENT_SEASON):
    """When strategies with unused transfers are excluded the baseline strategy will
    normally not be part of the tree. In that case save it first with this function.
    """
    # TODO: Discount factor, use season argument
    score = 0
    for gw in gameweeks:
        score += squad.get_expected_points(gw, tag)
    num_gameweeks = len(gameweeks)
    zeros = ("0-" * num_gameweeks)[:-1]
    filename = os.path.join(OUTPUT_DIR, "strategy_{}_{}.json".format(tag, zeros))
    with open(filename, "w") as f:
        json.dump({"total_score": score}, f)


def find_baseline_score_from_json(tag, num_gameweeks):
    """
    The baseline score is the one where we make 0 transfers
    for all gameweeks.
    """
    # the strategy string we're looking for will be something like '0-0-0'.
    zeros = ("0-" * num_gameweeks)[:-1]
    filename = os.path.join(OUTPUT_DIR, "strategy_{}_{}.json".format(tag, zeros))
    if not os.path.exists(filename):
        print("Couldn't find {}".format(filename))
        return 0.0
    else:
        with open(filename) as inputfile:
            strat = json.load(inputfile)
            score = strat["total_score"]
            return score


def print_strat(strat):
    """
    nicely formated printout as output of optimization.
    """

    gameweeks_as_str = strat["points_per_gw"].keys()
    gameweeks_as_int = sorted([int(gw) for gw in gameweeks_as_str])
    print(" ===============================================")
    print(" ========= Optimum strategy ====================")
    print(" ===============================================")
    for gw in gameweeks_as_int:
        print("\n =========== Gameweek {} ================\n".format(gw))
        print("Chips played:  {}\n".format(strat["chips_played"][str(gw)]))
        print("Players in:\t\t\tPlayers out:")
        print("-----------\t\t\t------------")
        for i in range(len(strat["players_in"][str(gw)])):
            pin = get_player_name(strat["players_in"][str(gw)][i])
            pout = get_player_name(strat["players_out"][str(gw)][i])
            if len(pin) < 20:
                subs = "{}\t\t\t{}".format(pin, pout)
            else:
                subs = "{}\t\t{}".format(pin, pout)
            print(subs)
    print("\n==========================")
    print(" Total score: {} \n".format(int(strat["total_score"])))
    pass


def print_team_for_next_gw(strat):
    """
    Display the team (inc. subs and captain) for the next gameweek
    """
    t = get_starting_squad()
    gameweeks_as_str = strat["points_per_gw"].keys()
    gameweeks_as_int = sorted([int(gw) for gw in gameweeks_as_str])
    next_gw = gameweeks_as_int[0]
    for pidout in strat["players_out"][str(next_gw)]:
        t.remove_player(pidout)
    for pidin in strat["players_in"][str(next_gw)]:
        t.add_player(pidin)
    tag = get_latest_prediction_tag()
    t.get_expected_points(next_gw, tag)
    print(t)


def run_optimization(
    gameweeks,
    tag,
    season=CURRENT_SEASON,
    fpl_team_id=None,
    chip_gameweeks={},
    num_free_transfers=None,
    max_total_hit=None,
    allow_unused_transfers=True,
    max_transfers=2,
    num_iterations=100,
    num_thread=4,
    profile=False,
):
    """
    This is the actual main function that sets up the multiprocessing
    and calls the optimize function for every num_transfers/gameweek
    combination, to find the best strategy.
    The chip-related variables e.g. wildcard_week are -1 if that chip
    is not to be played, 0 for 'play it any week', or the gw in which
    it should be played.
    """
    # How many free transfers are we starting with?
    if not num_free_transfers:
        num_free_transfers = get_free_transfers(gameweeks[0], fpl_team_id)
    # create the output directory for temporary json files
    # giving the points prediction for each strategy
    shutil.rmtree(OUTPUT_DIR, ignore_errors=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    # first get a baseline prediction
    # baseline_score, baseline_dict = get_baseline_prediction(num_weeks_ahead, tag)

    # Get a dict of what chips we definitely or possibly will play
    # in each gw
    chip_gw_dict = construct_chip_dict(gameweeks, chip_gameweeks)

    # create a queue that we will add nodes to, and some processes to take
    # things off it
    squeue = CustomQueue()
    procs = []
    # create one progress bar for each thread
    progress_bars = []
    for i in range(num_thread):
        progress_bars.append(tqdm(total=100))

    # number of nodes in tree will be something like 3^num_weeks unless we allow
    # a "chip" such as wildcard or free hit, in which case it gets complicated
    num_weeks = len(gameweeks)
    num_expected_outputs = count_expected_outputs(
        num_weeks,
        next_gw=gameweeks[0],
        free_transfers=num_free_transfers,
        max_total_hit=max_total_hit,
        allow_unused_transfers=allow_unused_transfers,
        max_transfers=max_transfers,
        chip_gw_dict=chip_gw_dict,
    )
    total_progress = tqdm(total=num_expected_outputs, desc="Total progress")

    # functions to be passed to subprocess to update or reset progress bars
    def reset_progress(index, strategy_string):
        if strategy_string == "DONE":
            progress_bars[index].close()
        else:
            progress_bars[index].n = 0
            progress_bars[index].desc = "strategy: " + strategy_string
            progress_bars[index].refresh()

    def update_progress(increment=1, index=None):
        if index is None:
            # outer progress bar
            nfiles = len(os.listdir(OUTPUT_DIR))
            total_progress.n = nfiles
            total_progress.refresh()
            if nfiles == num_expected_outputs:
                total_progress.close()
                for pb in progress_bars:
                    pb.close()
        else:
            progress_bars[index].update(increment)
            progress_bars[index].refresh()

    starting_squad = get_starting_squad()

    if not allow_unused_transfers and (
        num_weeks > 1 or (num_weeks == 1 and num_free_transfers == 2)
    ):
        # if we are excluding unused transfers the tree may not include the baseline
        # strategy. In those cases quickly calculate and save it here first.
        save_baseline_score(starting_squad, gameweeks, tag, season=season)
        update_progress()

    # Add Processes to run the the target 'optimize' function.
    # This target function needs to know:
    #  num_transfers
    #  current_team (list of player_ids)
    #  transfer_dict {"gw":<gw>,"in":[],"out":[]}
    #  total_score
    #  num_free_transfers
    #  budget
    for i in range(num_thread):
        processor = Process(
            target=optimize,
            args=(
                squeue,
                i,
                num_expected_outputs,
                gameweeks,
                season,
                tag,
                chip_gw_dict,
                max_total_hit,
                allow_unused_transfers,
                max_transfers,
                num_iterations,
                update_progress,
                reset_progress,
                profile,
            ),
        )
        processor.daemon = True
        processor.start()
        procs.append(processor)
    # add starting node to the queue
    squeue.put((0, num_free_transfers, 0, starting_squad, {}, "starting"))

    for i, p in enumerate(procs):
        progress_bars[i].close()
        progress_bars[i] = None
        p.join()

    # find the best from all the strategies tried
    best_strategy = find_best_strat_from_json(tag)

    baseline_score = find_baseline_score_from_json(tag, num_weeks)
    fill_suggestion_table(baseline_score, best_strategy, season)
    for i in range(len(procs)):
        print("\n")
    print("\n====================================\n")
    print("Baseline score: {}".format(baseline_score))
    print("Best score: {}".format(best_strategy["total_score"]))
    print_strat(best_strategy)
    print_team_for_next_gw(best_strategy)
    shutil.rmtree(OUTPUT_DIR, ignore_errors=True)
    return


def construct_chip_dict(gameweeks, chip_gameweeks):
    """
    Given a dict of form {<chip_name>: <chip_gw>,...}
    where <chip_name> is e.g. 'wildcard', and <chip_gw> is -1 if chip
    is not to be played, 0 if it is to be considered any week, or gw
    if it is definitely to be played that gw, return a dict
    { <gw>: {"chip_to_play": [<chip_name>],
             "chips_allowed": [<chip_name>,...]},...}
    """
    chip_dict = {}
    # first fill in any allowed chips
    for gw in gameweeks:
        chip_dict[gw] = {"chip_to_play": None, "chips_allowed": []}
        for k, v in chip_gameweeks.items():
            if int(v) == 0:
                chip_dict[gw]["chips_allowed"].append(k)
    # now go through again, for any definite ones, and remove
    # other allowed chips from those gameweeks
    for k, v in chip_gameweeks.items():
        if v > 0 and v in gameweeks:  # v is the gameweek
            # check we're not trying to play 2 chips
            if chip_dict[v]["chip_to_play"] is not None:
                raise RuntimeError(
                    "Cannot play {} and {} in the same week".format(
                        chip_dict[v]["chip_to_play"], k
                    )
                )
            chip_dict[v]["chip_to_play"] = k
            chip_dict[v]["chips_allowed"] = []
    return chip_dict


def sanity_check_args(args):
    """
    Check that command-line arguments are self-consistent.
    """
    if args.weeks_ahead and (args.gw_start or args.gw_end):
        raise RuntimeError("Please only specify weeks_ahead OR gw_start/end")
    elif (args.gw_start and not args.gw_end) or (args.gw_end and not args.gw_start):
        raise RuntimeError("Need to specify both gw_start and gw_end")
    if args.num_free_transfers and args.num_free_transfers not in range(1, 3):
        raise RuntimeError("Number of free transfers must be 1 or 2")
    return True


def main():
    """
    The main function, to be used as entrypoint.
    """
    parser = argparse.ArgumentParser(
        description="Try some different transfer strategies"
    )
    parser.add_argument("--weeks_ahead", help="how many weeks ahead", type=int)
    parser.add_argument("--gw_start", help="first gameweek to consider", type=int)
    parser.add_argument("--gw_end", help="last gameweek to consider", type=int)
    parser.add_argument("--tag", help="specify a string identifying prediction set")
    parser.add_argument(
        "--wildcard_week",
        help="play wildcard in the specified week. Choose 0 for 'any week'.",
        type=int,
        default=-1,
    )
    parser.add_argument(
        "--free_hit_week",
        help="play free hit in the specified week. Choose 0 for 'any week'.",
        type=int,
        default=-1,
    )
    parser.add_argument(
        "--triple_captain_week",
        help="play triple captain in the specified week. Choose 0 for 'any week'.",
        type=int,
        default=-1,
    )
    parser.add_argument(
        "--bench_boost_week",
        help="play bench_boost in the specified week. Choose 0 for 'any week'.",
        type=int,
        default=-1,
    )
    parser.add_argument(
        "--num_free_transfers", help="how many free transfers do we have", type=int
    )
    parser.add_argument(
        "--max_hit",
        help="maximum number of points to spend on additional transfers",
        type=int,
        default=8,
    )
    parser.add_argument(
        "--allow_unused",
        help="if set, include strategies that waste free transfers",
        action="store_true",
    )
    parser.add_argument(
        "--num_iterations",
        help="how many iterations to use for Wildcard/Free Hit optimization",
        type=int,
        default=100,
    )
    parser.add_argument(
        "--num_thread", help="how many threads to use", type=int, default=4
    )
    parser.add_argument(
        "--season",
        help="what season, in format e.g. '2021'",
        type=str,
        default=CURRENT_SEASON,
    )
    parser.add_argument(
        "--profile",
        help="For developers: Profile strategy execution time",
        action="store_true",
    )
    parser.add_argument(
        "--fpl_team_id",
        help="specify fpl team id",
        nargs=1,
        required=False,
        default=None,
    )
    args = parser.parse_args()

    fpl_team_id = args.fpl_team_id if args.fpl_team_id else None

    sanity_check_args(args)
    season = args.season
    if args.weeks_ahead:
        gameweeks = list(
            range(get_next_gameweek(), get_next_gameweek() + args.weeks_ahead)
        )
    else:
        gameweeks = list(range(args.gw_start, args.gw_end))
    num_iterations = args.num_iterations

    if args.num_free_transfers:
        num_free_transfers = args.num_free_transfers
    else:
        num_free_transfers = None  # will work it out in run_optimization
    if args.tag:
        tag = args.tag
    else:
        # get most recent set of predictions from DB table
        tag = get_latest_prediction_tag()
    max_total_hit = args.max_hit
    allow_unused_transfers = args.allow_unused
    num_thread = args.num_thread
    profile = args.profile if args.profile else False
    chip_gameweeks = {}
    chip_gameweeks["wildcard"] = args.wildcard_week
    chip_gameweeks["free_hit"] = args.free_hit_week
    chip_gameweeks["triple_captain"] = args.triple_captain_week
    chip_gameweeks["bench_boost"] = args.bench_boost_week

    if not check_tag_valid(tag, gameweeks, season=CURRENT_SEASON):
        print(
            "ERROR: Database does not contain predictions",
            "for all the specified optimsation gameweeks.\n",
            "Please run 'airsenal_run_prediction' first with the",
            "same input gameweeks and season you specified here.",
        )
        sys.exit(1)

    run_optimization(
        gameweeks,
        tag,
        season,
        fpl_team_id,
        chip_gameweeks,
        num_free_transfers,
        max_total_hit,
        allow_unused_transfers,
        2,
        num_iterations,
        num_thread,
        profile,
    )
