"""
Intersect vcf with reference simple repeats and report
how the an alternate allele affects the copies using TRF
"""
import os
import sys
import json
import types
import shutil
import logging
import argparse
import multiprocessing
from io import StringIO
from functools import cmp_to_key
from collections import defaultdict

import pysam
import truvari

trfshared = types.SimpleNamespace()

try:
    from setproctitle import setproctitle  # pylint: disable=import-error,useless-suppression
except ModuleNotFoundError:
    def setproctitle(_):
        """ dummy function """
        return



def iter_tr_regions(fn):
    """
    Read a repeats file with structure chrom, start, end, annotations.json
    returns generator of dicts
    """
    for line in truvari.opt_gz_open(fn):
        chrom, start, end, annos = line.strip().split('\t')
        start = int(start)
        end = int(end)
        annos = json.loads(annos)
        yield {'chrom': chrom,
               'start': start,
               'end': end,
               'annos': annos}


def parse_trf_output(fn):
    """
    Parse the outputs from TRF
    Returns a list of hits
    """
    trf_cols = [("start", int),
                ("end", int),
                ("period", int),
                ("copies", float),
                ("consize", int),
                ("pctmat", int),
                ("pctindel", int),
                ("score", int),
                ("A", int),
                ("C", int),
                ("G", int),
                ("T",  int),
                ("entropy", float),
                ("repeat", str),
                ("unk1", str),
                ("unk2", str),
                ("unk3", str)]
    ret = defaultdict(list)
    with open(fn, 'r') as fh:
        var_key = None
        while True:
            line = fh.readline().strip()
            if line == "":
                break
            if line.startswith("@"):
                var_key = line[1:]
                continue
            data = {x[0]: x[1](y) for x, y in zip(trf_cols, line.split(' '))}
            # 0-based correction
            data['start'] -= 1
            ret[var_key].append(data)
    return dict(ret)

def compare_scores(a, b):
    """
    sort annotations
    """
    # most amount of SV covered
    ret = 0
    if a['ovl_pct'] > b['ovl_pct']:
        ret = 1
    elif a['ovl_pct'] < b['ovl_pct']:
        ret = -1
    elif a['score'] > b['score']:
        ret = 1
    elif a['score'] < b['score']:
        ret = -1
    else:
        aspan = a['end'] - a['start']
        bspan = b['end'] = b['start']
        if aspan > bspan:
            ret = 1
        elif aspan < bspan:
            ret = -1
    return ret
score_sorter = cmp_to_key(compare_scores)

class TRFAnno():
    """
    Class for trf annotation
    Operates on a single TRF region across multiple TRF annotations
    """

    def __init__(self, region, reference,
                 executable="trf409.linux64",
                 trf_params="2 7 7 80 10 50 500 -m -f -h -d -ngs"):
        """ setup """
        self.region = region
        self.reference = reference
        self.executable = executable
        if "-ngs" not in trf_params:
            trf_params = trf_params + " -ngs "
        self.trf_params = trf_params

        self.known_motifs = {_['repeat']:_['copies'] for _ in self.region['annos']}

    def entry_to_key(self, entry):
        """
        VCF entries to names for the fa and header lines in the tr output
        returns the key and the entry's size
        """
        sz = truvari.entry_size(entry)
        svtype = truvari.entry_variant_type(entry)
        o_sz = sz if svtype == 'INS' else 0 # span of variant in alt-seq
        key = f"{entry.chrom}:{entry.start}:{entry.stop}:{o_sz}:{hash(entry.ref)}:{hash(entry.alts[0])}"
        return key, sz, svtype

    def make_seq(self, entry, svtype):
        """
        Make the haplotype sequence
        """
        # variant position relative to this region
        r_start = entry.start - self.region['start']
        r_end = entry.stop - self.region['start']

        up_seq = self.reference[:r_start]
        dn_seq = self.reference[r_end:]
        if svtype == "INS":
            m_seq = up_seq + entry.alts[0] + dn_seq
        elif svtype == "DEL":
            m_seq = up_seq + dn_seq
        else:
            logging.critical("Can only consider entries with 'SVTYPE' INS/DEL")
            sys.exit(1)
        return m_seq

    def build_seqs(self, entries, min_length=50, max_length=10000):
        """
        Write sequences into self.fa_fn
        returns the number of sequences and their total length
        """
        n_seqs = 0
        seq_l = 0
        with open(self.fa_fn, 'w') as fout:
            for entry in entries:
                key, sz, svtype = self.entry_to_key(entry)
                if sz < min_length:
                    continue
                seq = self.make_seq(entry, svtype)
                if min_length <= len(seq) <= max_length:
                    n_seqs += 1
                    seq_l += len(seq)
                    fout.write(f">{key}\n{seq}\n")
        return n_seqs, seq_l

    def run_trf(self, seq):
        """
        Given a sequence, run TRF and return result
        """
        fa_fn = truvari.make_temp_filename(suffix='.fa')
        tr_fn = fa_fn + '.txt'
        with open(fa_fn, 'w') as fout:
            fout.write(f">key\n{seq}\n")

        cmd = f"{self.executable} {fa_fn} {self.trf_params} > {tr_fn}"
        ret = truvari.cmd_exe(cmd)
        if ret.ret_code != 0:
            logging.error("Couldn't run trf. Check Parameters")
            logging.error(cmd)
            logging.error(str(ret))
            return []

        annos = parse_trf_output(tr_fn)
        if annos:
            annos = annos['key']
            for anno in annos:
                anno['start'] += self.region['start']
                anno['end'] += self.region['start']
        else:
            annos = []
        return annos

    def filter_annotations(self):
        """
        Pick the best annotation for every entry in self.unfilt_annotations
        places results into self.annotations
        I've put this into its own method because eventually, maybe, we can have options on how to choose
        """
        self.annotations = {}
        for var_key in self.unfilt_annotations:
            var_start, var_end, var_len = var_key.split(":")[1:4]
            var_start = int(var_start)
            var_end = int(var_end)
            var_len = int(var_len)
            scores = []
            for m_anno in self.unfilt_annotations[var_key]:
                m_sc = self.score_annotation(var_start, var_end, m_anno, True)
                if m_sc:
                    scores.append(m_sc)
            scores.sort(reverse=True, key=score_sorter)
            if scores:
                self.annotations[var_key] = scores[0][-1]

    def score_annotation(self, var_start, var_end, anno):
        """
        Scores the annotation. Addes fields in place.
        if is_new, we calculate the diff
        """
        ovl_pct = truvari.overlap_percent(var_start, var_end, anno['start'], anno['end'])
        # has to hit
        if ovl_pct <= 0:
            return None
        if anno['repeat'] in self.known_motifs:
            anno['diff'] = anno['copies'] - self.known_motifs[anno['repeat']]
        else:
            anno['diff'] = 0
        anno['ovl_pct'] = ovl_pct
        return anno

    def del_annotator(self, entry, svlen):
        """
        Annotate a deletion
        """
        var_start = entry.start
        var_end = entry.stop
        scores = []
        for anno in self.region['annos']:
            ovl_pct = truvari.overlap_percent(var_start, var_end, anno['start'], anno['end'])
            if ovl_pct == 0:
                continue
            m_sc = dict(anno)
            m_sc['ovl_pct'] = ovl_pct
            m_sc['diff'] = - (ovl_pct * svlen) / anno['period']
            scores.append(m_sc)
        scores.sort(reverse=True, key=score_sorter)
        if scores:
            return scores[0]
        return None

    def ins_annotator(self, entry):
        """
        Annotate an insertion
        """
        seq = self.make_seq(entry, 'INS')
        annos = self.run_trf(seq)
        scores = []
        for anno in annos:
            m_sc = self.score_annotation(entry.start, entry.stop, anno)
            if m_sc:
                scores.append(m_sc)
        scores.sort(reverse=True, key=score_sorter)
        if scores:
            return scores[0]
        return None

    def annotate(self, entry, min_length=50):
        """
        Figure out the hit
        """
        svtype = truvari.entry_variant_type(entry)
        sz = truvari.entry_size(entry)
        entry.info['TRF'] = True
        if sz < min_length:
            return
        repeat = None
        if svtype == 'DEL':
            repeat = self.del_annotator(entry, sz)
            # if it is inside a known repeat, do special math
            # otherwise, annotate as a new change
            # and also try to unite it with the knowns
        elif svtype == 'INS':
            repeat = self.ins_annotator(entry)

        if repeat:
            entry.info["TRFovl"] = round(repeat['ovl_pct'], 3)
            entry.info["TRFdiff"] = round(repeat['diff'], 1)
            entry.info["TRFperiod"] = repeat["period"]
            entry.info["TRFcopies"] = repeat["copies"]
            entry.info["TRFscore"] = repeat["score"]
            entry.info["TRFentropy"] = repeat["entropy"]
            entry.info["TRFrepeat"] = repeat["repeat"]

def process_tr_region(region):
    """
    Process vcf lines from a tr reference section
    """
    logging.debug(f"Starting region {region['chrom']}:{region['start']}-{region['end']}")
    setproctitle(f"trf {region['chrom']}:{region['start']}-{region['end']}")
    vcf = pysam.VariantFile(trfshared.args.input)
    to_consider = []
    try:
        for entry in vcf.fetch(region['chrom'], region['start'], region['end']):
            # Variants must be entirely contained within region
            if not (entry.start >= region['start'] and entry.stop < region['end']):
                continue
            # variant must be over minlength?
            to_consider.append(entry)
    except ValueError as e:
        logging.debug("Skipping VCF fetch %s", e)

    # no variants, so nothing to do
    if not to_consider:
        return ""

    ref = pysam.FastaFile(trfshared.args.reference)
    ref_seq = ref.fetch(region['chrom'], region['start'], region['end'])
    tanno = TRFAnno(region, ref_seq,
                    trfshared.args.executable,
                    trfshared.args.trf_params)

    new_header = edit_header(vcf.header)
    out = StringIO()
    for entry in to_consider:
        entry.translate(new_header)
        tanno.annotate(entry, trfshared.args.min_length)
        out.write(str(entry))
    out.seek(0)
    setproctitle(f"trf {region['chrom']}:{region['start']}-{region['end']}")
    logging.debug(f"Done region {region['chrom']}:{region['start']}-{region['end']}")
    return out.read()


def edit_header(header):
    """
    New VCF INFO fields
    """
    header = header.copy()
    header.add_line(('##INFO=<ID=TRF,Number=1,Type=Flag,'
                     'Description="Entry hits a simple repeat region">'))
    header.add_line(('##INFO=<ID=TRFdiff,Number=1,Type=Float,'
                     'Description="ALT TR copy difference from reference">'))
    header.add_line(('##INFO=<ID=TRFperiod,Number=1,Type=Integer,'
                     'Description="Period size of the repeat">'))
    header.add_line(('##INFO=<ID=TRFcopies,Number=1,Type=Float,'
                     'Description="Number of copies aligned with the consensus pattern">'))
    header.add_line(('##INFO=<ID=TRFscore,Number=1,Type=Integer,'
                     'Description="Alignment score">'))
    header.add_line(('##INFO=<ID=TRFentropy,Number=1,Type=Float,'
                     'Description="Entropy measure">'))
    header.add_line(('##INFO=<ID=TRFrepeat,Number=1,Type=String,'
                     'Description="Repeat motif">'))
    header.add_line(('##INFO=<ID=TRFovl,Number=1,Type=Float,'
                     'Description="Percent of ALT covered by TRF annotation">'))
    return header

def parse_args(args):
    """
    Pull the command line parameters
    """
    parser = argparse.ArgumentParser(prog="trf", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-i", "--input", type=str, required=True,
                        help="VCF to annotate")
    parser.add_argument("-o", "--output", type=str, default="/dev/stdout",
                        help="Output filename (stdout)")
    parser.add_argument("-e", "--executable", type=str, default="trf409.linux64",
                        help="Path to tandem repeat finder (%(default)s)")
    parser.add_argument("-T", "--trf-params", type=str, default="3 7 7 80 5 40 500 -h -ngs",
                        help="Default parameters to send to trf (%(default)s)")
    parser.add_argument("-r", "--repeats", type=str, required=True,
                        help="Reference repeat annotations")
    parser.add_argument("-f", "--reference", type=str, required=True,
                        help="Reference fasta file")
    parser.add_argument("-m", "--min-length", type=truvari.restricted_int, default=50,
                        help="Minimum size of entry to annotate (%(default)s)")
    parser.add_argument("-M", "--max-length", type=truvari.restricted_int, default=10000,
                        help="Maximum size of sequence to run through trf (%(default)s)")
    parser.add_argument("-t", "--threads", type=truvari.restricted_int, default=multiprocessing.cpu_count(),
                        help="Number of threads to use (%(default)s)")
    parser.add_argument("--debug", action="store_true",
                        help="Verbose logging")

    args = parser.parse_args(args)
    truvari.setup_logging(args.debug)
    return args

def check_params(args):
    """
    Ensure the files are compressed/indexed
    """
    check_fail = False
    if not os.path.exists(args.input):
        logging.error(f"{args.input} doesn't exit")
        check_fail = True
    if not args.input.endswith((".vcf.gz", ".bcf.gz")):
        logging.error(f"{args.input} isn't compressed vcf")
        check_fail = True
    if not os.path.exists(args.input + '.tbi') and not os.path.exists(args.input + '.csi'):
        logging.error(f"{args.input}[.tbi|.csi] doesn't exit")
        check_fail = True
    if not args.repeats.endswith(".bed.gz"):
        logging.error(f"{args.repeats} isn't compressed bed")
        check_fail = True
    if not os.path.exists(args.repeats + '.tbi'):
        logging.error(f"{args.repeats}.tbi doesn't exit")
        check_fail = True
    if not shutil.which(args.executable):
        logging.error(f"{args.executable} not found in path")
        check_fail = True
    if check_fail:
        logging.error("Please fix parameters")
        sys.exit(1)

def trf_single_main(cmdargs):
    """ TRF annotation """
    args = parse_args(cmdargs)
    check_params(args)
    trfshared.args = args

    m_lookup, _ = truvari.build_anno_tree(args.repeats)
    m_regions = iter_tr_regions(args.repeats)

    vcf = pysam.VariantFile(trfshared.args.input)
    new_header = edit_header(vcf.header)
    
    with open(args.output, 'w') as fout:
        fout.write(str(new_header))
        for i in m_regions:
            fout.write(process_tr_region(i))
    logging.info("Finished trf")

def trf_main(cmdargs):
    """ TRF annotation """
    args = parse_args(cmdargs)
    check_params(args)
    trfshared.args = args

    m_lookup, _ = truvari.build_anno_tree(args.repeats)
    m_regions = iter_tr_regions(args.repeats)

    vcf = pysam.VariantFile(trfshared.args.input)
    new_header = edit_header(vcf.header)

    with multiprocessing.Pool(args.threads, maxtasksperchild=1) as pool:
        chunks = pool.imap_unordered(process_tr_region, m_regions)
        pool.close()
        with open(args.output, 'w') as fout:
            fout.write(str(new_header))
            # Write variants not considered
            for entry in vcf:
                # If this is what's slow..... IDK
                hits = m_lookup[entry.chrom][entry.start:entry.stop]
                has_hit = False
                for i in hits:
                    if entry.start >= i.begin and entry.stop < i.end:
                        has_hit = True
                        break
                if not has_hit:
                    fout.write(str(entry))
            # Now collect the others
            for i in chunks:
                fout.write(i)
        pool.join()

    logging.info("Finished trf")


if __name__ == '__main__':
    trf_single_main(sys.argv[1:])
