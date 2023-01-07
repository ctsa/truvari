"""
Structural variant benchmarking tool
Given baseline and comparison sets of variants, calculate the recall/precision/f-measure
"""
import os
import sys
import copy
import json
import logging
import argparse
import itertools

from collections import defaultdict, OrderedDict, Counter

import pysam
import numpy as np

import truvari

class StatsBox(OrderedDict):
    """
    Make a blank stats box for counting TP/FP/FN and calculating performance
    """

    def __init__(self):
        super().__init__()
        self["TP-base"] = 0
        self["TP-comp"] = 0
        self["FP"] = 0
        self["FN"] = 0
        self["precision"] = 0
        self["recall"] = 0
        self["f1"] = 0
        self["base cnt"] = 0
        self["comp cnt"] = 0
        self["TP-comp_TP-gt"] = 0
        self["TP-comp_FP-gt"] = 0
        self["TP-base_TP-gt"] = 0
        self["TP-base_FP-gt"] = 0
        self["gt_concordance"] = 0
        self["gt_matrix"] = defaultdict(Counter)

    def calc_performance(self):
        """
        Calculate the precision/recall
        """
        if self["TP-base"] == 0 and self["FN"] == 0:
            logging.warning("No TP or FN calls in base!")
        elif self["TP-comp"] == 0 and self["FP"] == 0:
            logging.warning("No TP or FP calls in comp!")

        precision, recall, f1 = truvari.performance_metrics(self["TP-base"], self["TP-comp"], self["FN"], self["FP"])

        self["precision"] = precision
        self["recall"] = recall
        self["f1"] = f1
        if self["TP-comp_TP-gt"] + self["TP-comp_FP-gt"] != 0:
            self["gt_concordance"] = float(self["TP-comp_TP-gt"]) / (self["TP-comp_TP-gt"] +
                                                                     self["TP-comp_FP-gt"])



def pick_multi_matches(match_matrix):
    """
    Given a numpy array of MatchResults
    Pick each base/comp call's best match
    """
    ret = []
    for b_max in match_matrix.max(axis=1):
        b_max = copy.copy(b_max)
        b_max.comp = None
        ret.append(b_max)

    for c_max in match_matrix.max(axis=0):
        c_max = copy.copy(c_max)
        c_max.base = None
        ret.append(c_max)
    return ret

def pick_gtcomp_matches(match_matrix):
    """
    Given a numpy array of MatchResults
    Find upto allele count mumber of matches
    """
    ret = []
    base_cnt, comp_cnt = match_matrix.shape
    match_matrix = np.ravel(match_matrix)
    match_matrix.sort()
    used_comp = Counter()
    used_base = Counter()
    for match in match_matrix[::-1]:
        # No more matches to find
        if base_cnt == 0 and comp_cnt == 0:
            break
        b_key = str(match.base)
        c_key = str(match.comp)
        # This is a trick
        base_is_used = used_base[b_key] >= match.base_gt_count
        comp_is_used = used_comp[c_key] >= match.comp_gt_count
        # Only write the comp (FP)
        if base_cnt == 0 and not comp_is_used:
            to_process = copy.copy(match)
            to_process.base = None
            to_process.state = False
            to_process.multi = True
            comp_cnt -= 1
            if used_comp[c_key] == 0: # Only write as F if it hasn't been a T
                ret.append(to_process)
            used_comp[c_key] = 9
        # Only write the base (FN)
        elif comp_cnt == 0 and not base_is_used:
            to_process = copy.copy(match)
            to_process.comp = None
            to_process.state = False
            to_process.multi = True
            base_cnt -= 1
            if used_base[b_key] == 0: # Only write as F if it hasn't been a T
                ret.append(to_process)
            used_base[b_key] = 9
        # Write both (any state)
        elif not base_is_used and not comp_is_used:
            to_process = copy.copy(match)
            # Don't write twice
            if used_base[b_key] != 0:
                to_process.base = None
            if used_comp[c_key] != 0:
                to_process.comp = None

            used_base[b_key] += match.comp_gt_count
            used_comp[c_key] += match.base_gt_count
            # All used up
            if used_base[b_key] >= match.base_gt_count:
                base_cnt -= 1
            if used_comp[c_key] >= match.comp_gt_count:
                comp_cnt -= 1

            # Safety edge case check
            if to_process.base is not None or to_process.comp is not None:
                ret.append(to_process)
    return ret

def pick_single_matches(match_matrix):
    """
    Given a numpy array of MatchResults
    for each base, get its best match and record that the comp call can't be used anymore
    Once all best pairs are found, return the remaining
    """
    ret = []
    base_cnt, comp_cnt = match_matrix.shape
    match_matrix = np.ravel(match_matrix)
    match_matrix.sort()
    used_comp = set()
    used_base = set()
    for match in match_matrix[::-1]:
        # No more matches to find
        if base_cnt == 0 and comp_cnt == 0:
            break

        base_is_used = str(match.base) in used_base
        comp_is_used = str(match.comp) in used_comp
        # Only write the comp
        if base_cnt == 0 and not comp_is_used:
            to_process = copy.copy(match)
            to_process.base = None
            to_process.state = False
            to_process.multi = True
            comp_cnt -= 1
            used_comp.add(str(to_process.comp))
            ret.append(to_process)
        # Only write the base
        elif comp_cnt == 0 and not base_is_used:
            to_process = copy.copy(match)
            to_process.comp = None
            to_process.state = False
            to_process.multi = True
            base_cnt -= 1
            used_base.add(str(to_process.base))
            ret.append(to_process)
        # Write both
        elif not base_is_used and not comp_is_used:
            to_process = copy.copy(match)
            base_cnt -= 1
            used_base.add(str(to_process.base))
            comp_cnt -= 1
            used_comp.add(str(to_process.comp))
            ret.append(to_process)
    return ret

###############
# VCF editing #
###############
def edit_header(my_vcf):
    """
    Add INFO for new fields to vcf
    """
    header = my_vcf.header.copy()
    header.add_line(('##INFO=<ID=TruScore,Number=1,Type=Integer,'
                     'Description="Truvari score for similarity of match">'))
    header.add_line(('##INFO=<ID=PctSeqSimilarity,Number=1,Type=Float,'
                     'Description="Pct sequence similarity between this variant and its closest match">'))
    header.add_line(('##INFO=<ID=PctSizeSimilarity,Number=1,Type=Float,'
                     'Description="Pct size similarity between this variant and its closest match">'))
    header.add_line(('##INFO=<ID=PctRecOverlap,Number=1,Type=Float,'
                     'Description="Percent reciprocal overlap percent of the two calls\' coordinates">'))
    header.add_line(('##INFO=<ID=StartDistance,Number=1,Type=Integer,'
                     'Description="Distance of the base call\'s end from comparison call\'s start">'))
    header.add_line(('##INFO=<ID=EndDistance,Number=1,Type=Integer,'
                     'Description="Distance of the base call\'s end from comparison call\'s end">'))
    header.add_line(('##INFO=<ID=SizeDiff,Number=1,Type=Float,'
                     'Description="Difference in size of base and comp calls">'))
    header.add_line(('##INFO=<ID=GTMatch,Number=1,Type=Integer,'
                     'Description="Base/Comparison genotypes AC difference">'))
    header.add_line(('##INFO=<ID=MatchId,Number=1,Type=String,'
                     'Description="Id to help tie base/comp calls together {chunkid}.{baseid}.{compid}">'))
    header.add_line(('##INFO=<ID=Multi,Number=0,Type=Flag,'
                     'Description="Call is false due to non-multimatching">'))
    return header


def annotate_entry(entry, match, header):
    """
    Make a new entry with all the information
    """
    entry.translate(header)
    entry.info["PctSeqSimilarity"] = round(match.seqsim, 4) if match.seqsim is not None else None
    entry.info["PctSizeSimilarity"] = round(match.sizesim, 4) if match.sizesim is not None else None
    entry.info["PctRecOverlap"] = round(match.ovlpct, 4) if match.ovlpct is not None else None
    entry.info["SizeDiff"] = match.sizediff
    entry.info["StartDistance"] = match.st_dist
    entry.info["EndDistance"] = match.ed_dist
    entry.info["GTMatch"] = match.gt_match
    entry.info["TruScore"] = int(match.score) if match.score else None
    entry.info["MatchId"] = match.matid
    entry.info["Multi"] = match.multi


##########################
# Parameters and outputs #
##########################
def parse_args(args):
    """
    Pull the command line parameters
    """
    defaults = truvari.Matcher.make_match_params()
    parser = argparse.ArgumentParser(prog="bench", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-b", "--base", type=str, required=True,
                        help="Baseline truth-set calls")
    parser.add_argument("-c", "--comp", type=str, required=True,
                        help="Comparison set of calls")
    parser.add_argument("-o", "--output", type=str, required=True,
                        help="Output directory")
    parser.add_argument("-f", "--reference", type=str, default=None,
                        help="Indexed fasta used to call variants")
    parser.add_argument("--debug", action="store_true", default=False,
                        help="Verbose logging")
    parser.add_argument("--prog", action="store_true",
                        help="Turn on progress monitoring")

    thresg = parser.add_argument_group("Comparison Threshold Arguments")
    thresg.add_argument("-r", "--refdist", type=truvari.restricted_int, default=defaults.refdist,
                        help="Max reference location distance (%(default)s)")
    thresg.add_argument("-p", "--pctseq", type=truvari.restricted_float, default=defaults.pctseq,
                        help="Min percent sequence similarity. Set to 0 to ignore. (%(default)s)")
    thresg.add_argument("-B", "--minhaplen", type=truvari.restricted_int, default=defaults.minhaplen,
                        help="Minimum haplotype sequence length to create (%(default)s)")
    thresg.add_argument("-P", "--pctsize", type=truvari.restricted_float, default=defaults.pctsize,
                        help="Min pct allele size similarity (minvarsize/maxvarsize) (%(default)s)")
    thresg.add_argument("-O", "--pctovl", type=truvari.restricted_float, default=defaults.pctovl,
                        help="Min pct reciprocal overlap (%(default)s)")
    thresg.add_argument("-t", "--typeignore", action="store_true", default=defaults.typeignore,
                        help="Variant types don't need to match to compare (%(default)s)")
    thresg.add_argument("--dup-to-ins", action="store_true",
                        help="Assume DUP svtypes are INS (%(default)s)")
    thresg.add_argument("--use-lev", action="store_true",
                        help="Use the Levenshtein distance ratio instead of edlib editDistance ratio (%(default)s)")
    thresg.add_argument("-C", "--chunksize", type=truvari.restricted_int, default=defaults.chunksize,
                        help="Max reference distance to compare calls (%(default)s)")

    genoty = parser.add_argument_group("Genotype Comparison Arguments")
    genoty.add_argument("-g", "--gtcomp", action="store_true", default=defaults.gtcomp,
                        help="Compare genotypes and allow homozygous variants to be matched twice")
    genoty.add_argument("--bSample", type=str, default=None,
                        help="Baseline calls sample to use (first)")
    genoty.add_argument("--cSample", type=str, default=None,
                        help="Comparison calls sample to use (first)")

    filteg = parser.add_argument_group("Filtering Arguments")
    filteg.add_argument("-s", "--sizemin", type=truvari.restricted_int, default=defaults.sizemin,
                        help="Minimum variant size to consider from --comp (%(default)s)")
    filteg.add_argument("-S", "--sizefilt", type=truvari.restricted_int, default=None,
                        help="Minimum variant size to consider from --base (30)")
    filteg.add_argument("--sizemax", type=truvari.restricted_int, default=defaults.sizemax,
                        help="Maximum variant size to consider for comparison (%(default)s)")
    filteg.add_argument("--passonly", action="store_true", default=defaults.passonly,
                        help="Only consider calls with FILTER == PASS")
    filteg.add_argument("--no-ref", default=defaults.no_ref, choices=['a', 'b', 'c'],
                        help="Don't include 0/0 or ./. GT calls from all (a), base (b), or comp (c) vcfs (%(default)s)")
    filteg.add_argument("--includebed", type=str, default=None,
                        help="Bed file of regions in the genome to include only calls overlapping")
    filteg.add_argument("--extend", type=truvari.restricted_int, default=0,
                        help="Distance to allow comp entries outside of includebed regions (%(default)s)")
    filteg.add_argument("--multimatch", action="store_true", default=defaults.multimatch,
                        help=("Allow base calls to match multiple comparison calls, and vice versa. "
                              "Output vcfs will have redundant entries. (%(default)s)"))

    args = parser.parse_args(args)
    # When sizefilt is not provided and sizemin has been lowered below the default,
    # set sizefilt to sizemin. Otherwise, if sizefilt not provided, set to default
    # This just makes it easier to specify e.g. `-s 1` instead of `-s 1 -S 1`
    if args.sizefilt is None:
        if args.sizemin < defaults.sizefilt:
            args.sizefilt = args.sizemin
        else:
            args.sizefilt = defaults.sizefilt

    # Setup abspaths
    args.base = os.path.abspath(args.base)
    args.comp = os.path.abspath(args.comp)
    args.includebed = os.path.abspath(args.includebed) if args.includebed else args.includebed
    args.reference = os.path.abspath(args.reference) if args.reference else args.reference

    return args


def check_params(args):
    """
    Checks parameters as much as possible.
    All errors are written to stderr without logging since failures mean no output
    """
    check_fail = False
    if args.chunksize < args.refdist:
        logging.error("--chunksize must be >= --refdist")
        check_fail = True
    if args.extend and args.includebed is None:
        logging.error("--extend can only be used when --includebed is set")
        check_fail = True
    if os.path.isdir(args.output):
        logging.error("Output directory '%s' already exists", args.output)
        check_fail = True
    if not os.path.exists(args.comp):
        logging.error("File %s does not exist", args.comp)
        check_fail = True
    if not os.path.exists(args.base):
        logging.error("File %s does not exist", args.base)
        check_fail = True
    if not args.comp.endswith(".gz"):
        logging.error(
            "Comparison vcf %s does not end with .gz. Must be bgzip'd", args.comp)
        check_fail = True
    if not os.path.exists(args.comp + '.tbi'):
        logging.error(
            "Comparison vcf index %s.tbi does not exist. Must be indexed", args.comp)
        check_fail = True
    if not args.base.endswith(".gz"):
        logging.error(
            "Base vcf %s does not end with .gz. Must be bgzip'd", args.base)
        check_fail = True
    if not os.path.exists(args.base + '.tbi'):
        logging.error(
            "Base vcf index %s.tbi does not exist. Must be indexed", args.base)
        check_fail = True
    if args.includebed and not os.path.exists(args.includebed):
        logging.error("Include bed %s does not exist", args.includebed)
        check_fail = True
    if args.reference and not os.path.exists(args.reference):
        logging.error("Reference %s does not exist", args.reference)
        check_fail = True
    return check_fail


def check_sample(vcf_fn, sample_id=None):
    """
    Checks that a sample is inside a vcf
    Returns True if check failed
    """
    vcf_file = pysam.VariantFile(vcf_fn)
    check_fail = False
    if sample_id is not None and sample_id not in vcf_file.header.samples:
        logging.error("Sample %s not found in vcf (%s)", sample_id, vcf_fn)
        check_fail = True
    if len(vcf_file.header.samples) == 0:
        logging.error("No SAMPLE columns found in vcf (%s)", vcf_fn)
        check_fail = True
    if sample_id is None:
        sample_id = vcf_file.header.samples[0]
    return check_fail, sample_id


def check_inputs(args):
    """
    Checks the inputs to ensure expected values are found inside of files
    Returns True if check failed
    """
    b_check, args.bSample = check_sample(args.base, args.bSample)
    c_check, args.cSample = check_sample(args.comp, args.cSample)
    return b_check or c_check


class BenchOutput():
    """
    Makes all of the output files for a Bench.run
    """
    def __init__(self, bench, matcher):
        """
        initialize
        """
        self.m_bench = bench
        self.m_matcher = matcher

        os.mkdir(self.m_bench.outdir)
        param_dict = self.m_bench.param_dict()
        param_dict.update(vars(self.m_matcher.params))

        if self.m_bench.do_logging:
            truvari.setup_logging(self.m_bench.debug, truvari.LogFileStderr(
                os.path.join(self.m_bench.outdir, "log.txt")), show_version=True)
            logging.info("Params:\n%s", json.dumps(param_dict, indent=4))

        with open(os.path.join(self.m_bench.outdir, 'params.json'), 'w') as fout:
            json.dump(param_dict, fout)

        in_vcf = pysam.VariantFile(self.m_bench.base_vcf)
        self.n_base_header = edit_header(in_vcf)
        in_vcf = pysam.VariantFile(self.m_bench.comp_vcf)
        self.n_comp_header = edit_header(in_vcf)

        self.tpb_out = pysam.VariantFile(os.path.join(self.m_bench.outdir, "tp-base.vcf"),
                                         mode='w', header=self.n_base_header)
        self.tpc_out = pysam.VariantFile(os.path.join(self.m_bench.outdir, "tp-comp.vcf"),
                                         mode='w', header=self.n_comp_header)

        self.fn_out = pysam.VariantFile(os.path.join(self.m_bench.outdir, "fn.vcf"),
                                        mode='w', header=self.n_base_header)
        self.fp_out = pysam.VariantFile(os.path.join(self.m_bench.outdir, "fp.vcf"),
                                        mode='w', header=self.n_comp_header)

        self.stats_box = StatsBox()

    def write_match(self, match, sizemin):
        """
        Annotate a MatchResults' entries then write to the apppropriate file
        and do the stats counting.
        Writer is responsible for handling FPs between sizefilt-sizemin
        """
        box = self.stats_box
        if match.base:
            box["base cnt"] += 1
            annotate_entry(match.base, match, self.n_base_header)
            if match.state:
                gtBase = str(match.base_gt)
                gtComp = str(match.comp_gt)
                box["gt_matrix"][gtBase][gtComp] += 1

                box["TP-base"] += 1
                self.tpb_out.write(match.base)
                if match.gt_match == 0:
                    box["TP-base_TP-gt"] += 1
                else:
                    box["TP-base_FP-gt"] += 1
            else:
                box["FN"] += 1
                self.fn_out.write(match.base)

        if match.comp:
            annotate_entry(match.comp, match, self.n_comp_header)
            if match.state:
                box["comp cnt"] += 1
                box["TP-comp"] += 1
                self.tpc_out.write(match.comp)
                if match.gt_match == 0:
                    box["TP-comp_TP-gt"] += 1
                else:
                    box["TP-comp_FP-gt"] += 1
            elif truvari.entry_size(match.comp) >= sizemin:
                # The if is because we don't count FPs between sizefilt-sizemin
                box["comp cnt"] += 1
                box["FP"] += 1
                self.fp_out.write(match.comp)

    def close_outputs(self):
        """
        Close all the files
        """
        self.tpb_out.close()
        self.tpc_out.close()
        self.fn_out.close()
        self.fp_out.close()
        truvari.compress_index_vcf(self.tpb_out.filename.decode())
        truvari.compress_index_vcf(self.tpc_out.filename.decode())
        truvari.compress_index_vcf(self.fn_out.filename.decode())
        truvari.compress_index_vcf(self.fp_out.filename.decode())

class Bench():
    """
    Object to perform a run of truvari bench
    """
    def __init__(self, base_vcf, comp_vcf, outdir, includebed=None, extend=0, debug=False , do_logging=False):
        """
        Initilize
        """
        self.base_vcf = base_vcf
        self.comp_vcf = comp_vcf
        self.outdir = outdir
        self.includebed = includebed
        self.extend = extend
        self.debug = debug
        self.do_logging = do_logging

    def param_dict(self):
        """
        Returns the parameters as a dict
        """
        return {'base': self.base_vcf,
                "comp": self.comp_vcf,
                "output": self.outdir,
                "includebed": self.includebed,
                "extend": self.extend,
                "debug": self.debug}

    def run(self, matcher):
        """
        Runs bench and returns the resulting :class:`truvari.BenchOutput`
        """
        output = BenchOutput(self, matcher)

        base = pysam.VariantFile(self.base_vcf)
        comp = pysam.VariantFile(self.comp_vcf)

        regions = truvari.RegionVCFIterator(base, comp,
                                            self.includebed,
                                            matcher.params.sizemax)
        regions.merge_overlaps()
        regions_extended = regions.extend(self.extend) if self.extend else regions

        base_i = regions.iterate(base)
        comp_i = regions_extended.iterate(comp)

        chunks = truvari.chunker(matcher, ('base', base_i), ('comp', comp_i))
        for match in itertools.chain.from_iterable(map(self.compare_chunk, chunks)):
            # setting non-matched comp variants that are not fully contained in the original regions to None
            # These don't count as FP or TP and don't appear in the output vcf files
            if self.extend and match.comp is not None and not match.state and not regions.include(match.comp):
                match.comp = None
            output.write_match(match, matcher.params.sizemin)

        with open(os.path.join(self.outdir, "summary.json"), 'w') as fout:
            output.stats_box.calc_performance()
            fout.write(json.dumps(output.stats_box, indent=4))

        output.close_outputs()
        return output

    @staticmethod
    def build_matrix(base_variants, comp_variants, matcher, chunk_id=0):
        """
        Builds MatchResults, returns them as a numpy matrix if there's at least one base and one comp variant
        otherwise, returns a list of the variants placed in MatchResuls
        """
        # All FPs
        if len(base_variants) == 0:
            fps = []
            for cid, c in enumerate(comp_variants):
                ret = truvari.MatchResult()
                ret.comp = c
                ret.matid = f"{chunk_id}._.{cid}"
                fps.append(ret)
                logging.debug("All FP -> %s", ret)
            return fps

        # All FNs
        if len(comp_variants) == 0:
            fns = []
            for bid, b in enumerate(base_variants):
                ret = truvari.MatchResult()
                ret.base = b
                ret.matid = f"{chunk_id}.{bid}._"
                logging.debug("All FN -> %s", ret)
                fns.append(ret)
            return fns

        # Build all v all matrix
        match_matrix = []
        for bid, b in enumerate(base_variants):
            base_matches = []
            for cid, c in enumerate(comp_variants):
                mat = matcher.build_match(b, c, f"{chunk_id}.{bid}.{cid}")
                logging.debug("Made mat -> %s", mat)
                base_matches.append(mat)
            match_matrix.append(base_matches)

        # For building custom pickers
        return np.array(match_matrix)

    @staticmethod
    def pick_matches(match_matrix, matcher):
        """
        Sort and annotate matches, returns a list
        """
        ret = []
        if matcher.params.multimatch:
            ret = pick_multi_matches(match_matrix)
        elif matcher.params.gtcomp:
            ret = pick_gtcomp_matches(match_matrix)
        else:
            ret = pick_single_matches(match_matrix)
        return ret

    def compare_chunk(self, chunk):
        """
        Given a filtered chunk, (from chunker) compare all of the calls
        """
        matcher, chunk_dict, chunk_id = chunk
        logging.debug("Comparing chunk %s", chunk_id)
        match_matrix = self.build_matrix(chunk_dict["base"], chunk_dict["comp"], matcher, chunk_id)
        if isinstance(match_matrix, list):
            return match_matrix

        return self.pick_matches(match_matrix, matcher)


def bench_main(cmdargs):
    """
    Main - entry point from command line
    """
    args = parse_args(cmdargs)

    if check_params(args) or check_inputs(args):
        sys.stderr.write("Couldn't run Truvari. Please fix parameters\n")
        sys.exit(100)

    matcher = truvari.Matcher(args)

    m_bench = Bench(args.base, args.comp, args.output, args.includebed, args.extend, args.debug, True)
    output = m_bench.run(matcher)

    logging.info("Stats: %s", json.dumps(output.stats_box, indent=4))
    logging.info("Finished bench")
