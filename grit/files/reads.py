import sys, os
from itertools import chain
from collections import defaultdict, namedtuple
from copy import copy

import pysam
import numpy

import grit.config as config
import junctions

ReadData = namedtuple('ReadData', [
        'strand', 'read_len', 'read_grp', 'map_prb', 'cov_regions'])

DEBUG = False

def clean_chr_name( chrm ):
    if chrm.startswith( "chr" ):
        chrm = chrm[3:]
    # convert the dmel chrm to M, to be consistent with ucsc
    if chrm.endswith( 'mitochondrion_genome' ):
        chrm = "M"
    return chrm

def fix_chrm_name_for_ucsc( chrm ):
    return 'chr' + clean_chr_name(chrm)

def guess_strand_from_fname( fname ):
    if fname.lower().rfind( "plus" ) >= 0:
        return '+'
    elif fname.lower().rfind( "+" ) >= 0:
        return '+'
    elif fname.lower().rfind( "minus" ) >= 0:
        return '-'
    elif fname.lower().rfind( "-" ) >= 0:
        return '-'
    else:
        raise ValueError, "Couldn't infer strand from filename '%s'" % fname
    
    assert False

def get_chrm( read, bam_obj ):
    chrm = bam_obj.getrname( read.tid )
    chrm = clean_chr_name( chrm )
    return chrm

def get_strand( read, reverse_read_strand, pairs_are_opp_strand ):
    # make sure that the read is on the correct strand
    if read.is_paired:
        if pairs_are_opp_strand  \
                and ( ( read.is_read1 and not read.is_reverse ) \
                          or ( read.is_read2 and read.is_reverse ) ):
            strand = '+'
        elif not pairs_are_opp_strand and not read.is_reverse:
            strand = '+'
        else:
            strand = '-'
    else:
        if not read.is_reverse:
            strand = '+'
        else:
            strand = '-'
    
    if reverse_read_strand:
        if strand == '+':
            strand = '-'
        else:
            strand = '+'

    return strand

def get_read_group( r1, r2 ):        
    return 'mean'
    r1_read_group = [ val for key, val in r1.tags if key == 'RG' ]
    r1_read_group = r1_read_group[0] if len( r1_read_group ) == 1 else 'mean'
    r2_read_group = [ val for key, val in r2.tags if key == 'RG' ]
    r2_read_group = r2_read_group[0] if len( r2_read_group ) == 1 else 'mean'
    if r1_read_group == r2_read_group:
        return r1_read_group
    else: 
        print "WARNING: Read groups do not match."
        return None


def get_rd_posterior_prb(read):
    # try to use the (statmap) posterior probability XP tag
    try: 
        map_prb = float(read.opt('XP'))
        # if this could be a posterior proability return it
        if 0.0 <= map_prb <= 1.:
            return map_prb
    # if this doesn't exist
    except KeyError: 
        pass
    # or it isn't a float
    except ValueError:
        pass
    
    # if we don't have a proper posterior probability, then
    # 1/number of mappings
    try: 
        map_prb = 1./read.opt('NH')
    except KeyError: 
        pass

    # if we have nothing, assume that it's just 1.
    return 1.0

def read_pairs_are_on_same_strand( bam_obj, min_num_reads_to_check=50000, 
                                   max_num_reads_to_check=100000 ):
    # keep track of which fractiona re on the sam strand
    paired_cnts = {'no_mate': 0, 'same_strand': 1e-4, 'diff_strand': 1e-4}
    
    num_good_reads = 0
    num_observed_reads = 0
    for read in bam_obj: 
        num_observed_reads += 1
        if num_observed_reads > max_num_reads_to_check:
            break
        
        if read.is_paired and read.mate_is_unmapped:
            continue
        
        map_prb = get_rd_posterior_prb(read)
        if map_prb < 0.99: continue
        
        if not read.is_paired:
            paired_cnts['no_mate'] += 1        
        elif read.is_reverse != read.mate_is_reverse:
            paired_cnts['diff_strand'] += 1
        else:
            paired_cnts['same_strand'] += 1
        # keep collecting reads until we observe enough
        num_good_reads += 1
        if num_good_reads > min_num_reads_to_check \
                and num_good_reads%min_num_reads_to_check == 0:
            # if the reads are single ended, then return True ( 
            #    because it doesnt really matter )
            if paired_cnts['no_mate'] >= 0.95*num_good_reads:
                return True
            if float(paired_cnts['same_strand'])/paired_cnts['diff_strand'] > 5:
                return True
            elif float(paired_cnts['diff_strand'])/paired_cnts['same_strand'] > 5:
                return False
    
    # if we have run out of reads, see if we can build the statistic
    if paired_cnts['no_mate'] >= 0.95*num_good_reads:
        return True
    if float(paired_cnts['same_strand'])/paired_cnts['diff_strand'] > 5:
        return True
    elif float(paired_cnts['diff_strand'])/paired_cnts['same_strand'] > 5:
        return False
    
    print >> sys.stderr, "Paired Cnts:", paired_cnts, "Num Reads", num_observed_reads
    raise ValueError, "Reads appear to be a mix of reads on the same and different strands. (%s)" % paired_cnts

def iter_coverage_intervals_for_read(read):
    # we loop through each contig in the cigar string to deal
    # with junctions reads.
    # note that the bam files are 0 based
    start = read.pos
    for contig_type, length in read.cigar:
        # if this is a match, add it 
        if contig_type == 0:
            yield ( start, start + length - 1 )
            start += length
        # skip reference insertions
        elif contig_type == 1:
            pass
            # start += length
        # move past refernce deletions
        elif contig_type == 2:
            start += length
        # skip past skipped regions
        elif contig_type == 3:
            start += length
        # since read positions dont include clipped regions, 
        # ignore clipping
        elif contig_type == 4 or contig_type == 5:
            pass
        else:
            print >> sys.stderr, "Unrecognized cigar format:", read.cigar

    return

def iter_coverage_regions_for_read( 
    read, bam_obj, reverse_read_strand, pairs_are_opp_strand ):
    """Find the regions covered by this read

    """
    strand = get_strand( read, reverse_read_strand, pairs_are_opp_strand )

    # get the chromosome, correcting for alternate chrm names
    chrm = get_chrm( read, bam_obj )

    for start, stop in iter_coverage_intervals_for_read( read ):
        yield chrm, strand, start, stop
    
    return

def extract_jns_and_reads_in_region((chrm, strand, r_start, r_stop), reads):
    assert strand in '+-.', "Strand must be -, +, or . for either"
    
    pair1_reads = defaultdict(list)
    pair2_reads = defaultdict(list)
    jn_reads = {'+': defaultdict(int), '-': defaultdict(int)}
    
    for read, rd_strand in reads.iter_reads_and_strand(
            chrm, r_start, r_stop+1):
        for jn in junctions.iter_jns_in_read(read):
            # skip jns whose start does not overlap this region, we subtract one
            # because the start refers to the first covered intron base, and 
            # we are talking about covered regions
            if jn[0]-1 < r_start or jn[0]-1 > r_stop: continue
            jn_reads[rd_strand][jn] += 1
        # if this is an anti-strand read, then we only care about the jns
        if strand != '.' and rd_strand != strand: continue
        
        # extract the information we care about:
        # -strand, alread have this
        # -regions covered
        cov_regions = list(iter_coverage_intervals_for_read(read))
        # -read length
        read_len = read.inferred_length
        # -read group
        try: read_grp = read.opt('RG')
        except KeyError: read_grp = 'mean'
        # -probability that the read originated in this location
        # if we can't find it, assume that it's uniform over alternate
        # mappings. If we can't find that, then assume that it's unique
        map_prb = get_rd_posterior_prb(read)
        
        # store the read data - we will join them later
        read_data = ReadData(rd_strand, read_len, read_grp, map_prb, cov_regions)
        if read.is_read1: pair1_reads[read.qname].append(read_data)
        else: pair2_reads[read.qname].append(read_data)
    
    return pair1_reads, pair2_reads, jn_reads['+'], jn_reads['-']

def calc_frag_len_from_read_data(read1_data, read2_data):
    frag_start = min(min(read1_data.cov_regions[0]), 
                     min(read2_data.cov_regions[0]))
    frag_stop = max(max(read1_data.cov_regions[0]), 
                    max(read2_data.cov_regions[0]))
    return frag_stop - frag_start + 1

def iter_paired_reads(p1_reads, p2_reads, fl_dist=None):
    for qname, r1_mappings_data in p1_reads.iteritems():
        if qname not in p2_reads: continue
        r2_mappings_data = p2_reads[qname]
        paired_reads = []
        post_prb_sum = 0.0
        for r1_data in r1_mappings_data:
            for r2_data in r2_mappings_data:
                if r1_data.strand != r2_data.strand: continue
                assert r1_data.read_grp == r1_data.read_grp
                flen = calc_frag_len_from_read_data(r1_data, r2_data)
                # if there is no fragment length data, then 
                # assume that the fragment sizes are all equally likely
                post_prb = r1_data.map_prb*r2_data.map_prb
                if fl_dist != None: assert False
                post_prb_sum += post_prb
                paired_reads.append( [
                        qname, r1_data.strand, r1_data.read_grp, flen, 
                        r1_data, r2_data, post_prb])
        for x in paired_reads: x[-1] = x[-1]/post_prb_sum
        yield qname, paired_reads

    return

def get_contigs_and_lens( reads_files ):
    """Get contigs and their lengths from a set of bam files.
    
    We make sure that the contig lengths are consistent in all of the bam files, and
    we remove contigs that dont have at least 1 read in at least one rnaseq file 
    and one promoter reads file.
    """
    chrm_lengths = {}
    contigs = None
    for bam in reads_files:
        bam_contigs = set()
        for ref_name, ref_len in zip(bam.references, bam.lengths):
            # add the contig to the chrm lengths file, checking to
            # make sure that lengths match if it has already been added
            if clean_chr_name( ref_name ) not in chrm_lengths:
                chrm_lengths[clean_chr_name( ref_name )] = ref_len
            else:
                assert chrm_lengths[clean_chr_name(ref_name)] == ref_len, \
                    "Chromosome lengths do not match between bam files"
            bam_contigs.add( clean_chr_name(ref_name) )
        
        if contigs == None:
            contigs = bam_contigs
        else:
            contigs = contigs.intersection( bam_contigs )
    
    # remove contigs that dont have reads in at least one file
    def at_least_one_bam_has_reads( chrm, bams ):
        for bam in reads_files:
            try:
                next( bam.fetch( chrm ) )
            except StopIteration:
                continue
            except KeyError:
                continue
            else:
                return True
    
    # produce the final list of contigs
    rv =  {}
    for key, val in chrm_lengths.iteritems():
        if key in contigs and any( 
            at_least_one_bam_has_reads(key, reads) for reads in reads_files ):
            rv[key] = val

    rv = zip(*sorted(rv.iteritems()))
    if len(rv) == 0:
        raise ValueError, "The bam files don't contain the same chromosome set.\nHint: make sure that the reads have been mapped to the same reference (this can be viewed with a call to samtools idxstats)"
    return rv

class MergedReads( object ):
    """Replicate the reads functionality for multiple underlying bams.
    
    """
    def _find_reads_type( self, reads ):
        if isinstance(reads, RNAseqReads):
            return "RNASeq"
        elif isinstance(reads, CAGEReads):
            return "CAGE"
        elif isinstance(reads, PolyAReads):
            return "PolyA"
        elif isinstance(reads, RAMPAGEReads):
            return "RAMPAGE" 
        elif isinstance(reads, Reads):
            return "Generic"
        else:
            raise ValueError, "Unrecognized read subtype %s" % type(reads)
    
    def __init__(self, all_reads):
        self._reads = list(all_reads)
        self.type = self._find_reads_type(self._reads[0])
        if not all( self.type == self._find_reads_type(reads)
                    for reads in self._reads ):
            raise ValueError, "All read objects must be the same type"
        
        self.references, self.lengths = get_contigs_and_lens( self._reads )
        
        return
    
    def mate(self, rd):
        for reads in self._reads:
            f_pos = reads.tell()
            mate = reads.mate(rd)
            reads.seek(f_pos)
            return mate
    
    @property
    def mapped(self):
        return sum( reads.mapped for reads in self._reads )
    
    def fetch(*args, **kwargs):
        # this should be true because self is implicitly the first argument
        assert len(args) > 0
        self, args = (args[0], args[1:])
        return chain(*[reads.fetch(*args, **kwargs) 
                       for reads in self._reads])
    
    def iter_reads( self, chrm, strand, start=None, stop=None ):
        for reads in self._reads:
            for rd in reads.iter_reads( chrm, strand, start, stop  ):
                yield rd    
        return

    def iter_reads_and_strand( self, chrm, start=None, stop=None ):
        for reads in self._reads:
            for res in reads.iter_reads_and_strand( chrm, start, stop  ):
                yield res    
        return
    
    def iter_paired_reads( self, chrm, strand, start, stop ):
        for reads in self._reads:
            for rd1, rd2 in reads.iter_paired_reads(chrm, strand, start, stop):
                yield rd1, rd2
        return
    
    def build_read_coverage_array( self, chrm, strand, 
                                   start, stop, read_pair=None ):
        assert stop >= start
        full_region_len = stop - start + 1
        cvg = numpy.zeros(full_region_len)
        for reads in self._reads:
            cvg += reads.build_read_coverage_array( 
                chrm, strand, start, stop, read_pair )
        
        return cvg

    def reload( self ):
        new_reads = [ reads.reload() for reads in self._reads ]
        return MergedReads( new_reads )

class TranscriptMappedReads( pysam.Samfile ):
    pass


class Reads( pysam.Samfile ):
    """Subclass the samfile object to include a method that returns reads 
       and their pairs.


    """ 
    def _build_chrm_mapping(self):
        self._canonical_to_chrm_name_mapping = {}
        for ref_name in self.references:
            self._canonical_to_chrm_name_mapping[clean_chr_name(ref_name)] =\
                ref_name
        
        return
    
    def contig_len( self, contig ):
        try:
            return self._contig_lens[self.fix_chrm_name(contig)]
        except AttributeError:
            self._contig_lens = dict( zip(self.references, self.lengths) )
            return self._contig_lens[self.fix_chrm_name(contig)]

    def determine_reverse_read_strand_param( 
            self, ref_genes, pairs_are_opp_strand, element_to_search,
            MIN_NUM_READS_PER_GENE, MIN_GENES_TO_CHECK):
        self._build_chrm_mapping()
        
        cnt_diff_strand = 0
        cnt_same_strand = 0
        for gene in ref_genes:
            reads_match = {True: 0, False: 0}
            exons = gene.extract_elements()[element_to_search]
            for start, stop in exons:
                for rd in self.fetch(gene.chrm, max(0, start), stop):
                    rd_strand = get_strand(rd, False, pairs_are_opp_strand)
                    if gene.strand == rd_strand: reads_match[True] += 1
                    else: reads_match[False] += 1
            
            # make sure that we have at least MIN_NUM_READS_PER_GENE 
            # reads in this gene
            if sum(reads_match.values()) < MIN_NUM_READS_PER_GENE: continue
            
            if reads_match[True] > 10*reads_match[False]:
                cnt_same_strand += 1
            if reads_match[False] > 10*reads_match[True]:
                cnt_diff_strand += 1
            
            # if we've succesfully explored enough genes, then return
            if cnt_same_strand > MIN_GENES_TO_CHECK and cnt_same_strand > 5*cnt_diff_strand: 
                return False
            if cnt_diff_strand > MIN_GENES_TO_CHECK and cnt_diff_strand > 5*cnt_same_strand: 
                return True
        
        assert False, "Could not auto determine 'reverse_read_strand' parameter for '%s' - the read strand parameter should be set in the control file" % self.filename
            
    
    def init(self, reads_are_paired, pairs_are_opp_strand, 
                   reads_are_stranded, reverse_read_strand ):
        self._init_kwargs = {
            'reads_are_paired': reads_are_paired,
            'pairs_are_opp_strand': pairs_are_opp_strand, 
            'reads_are_stranded': reads_are_stranded, 
            'reverse_read_strand': reverse_read_strand 
        }
        
        assert self.is_indexed()
        self._build_chrm_mapping()
        
        self.reads_are_paired = reads_are_paired
        
        self.pairs_are_opp_strand = pairs_are_opp_strand
        self.PAOS = self.pairs_are_opp_strand

        self.reads_are_stranded = reads_are_stranded
        
        self.reverse_read_strand = reverse_read_strand        
        self.RRR = reverse_read_strand
        
        try:
            self.fetch( self.references[0], 10000 )
        except ValueError, inst:
            raise ValueError, "BAM Files must be indexed."
        self.seek(0)
        
        return self

    def fix_chrm_name( self, chrm_name ):
        return self._canonical_to_chrm_name_mapping[clean_chr_name(chrm_name)]
    
    
    def fetch(*args, **kwargs):
        """Wrap fetch to fix the chrm name.

        """
        self = args[0]
        args = list( args )
        try:
            if len(args) > 1:
                args[1] = self.fix_chrm_name( args[1] )
            elif 'reference' in kwargs:
                kwargs['reference'] = self.fix_chrm_name( kwargs['reference'] )
        # if this contig doesn't exist for these reads, then 
        # return an empty iterator
        except KeyError:
            return ()
        return ( rd for rd in pysam.Samfile.fetch( *args, **kwargs )
                 if not rd.is_duplicate )
    
    def is_indexed( self ):
        return True
    
    def get_strand(self, read):
        return get_strand( 
            read, self.reverse_read_strand, self.pairs_are_opp_strand )
    
    def iter_reads_and_strand( self, chrm, start=None, stop=None ):
        for read in self.fetch( chrm, start, stop  ):
            rd_strand = self.get_strand(read)
            yield read, rd_strand
        return

    def iter_reads( self, chrm, strand, start=None, stop=None ):
        for read, rd_strand in self.iter_reads_and_strand( chrm, start, stop  ):
            if strand == None or rd_strand == strand:
                yield read        
        return

    def iter_paired_reads( self, chrm, strand, start, stop ):
        # whether or not the gene is on the positive strand
        gene_strnd_is_rev = ( strand == '-' )
        chrm = clean_chr_name( chrm )

        # get all of the first pairs
        def iter_pair1_reads():
            for read in self.iter_reads(chrm, strand, start, stop):
                if read.is_read1: 
                    yield read
        
        # index the pair 2 reads
        reads_pair2 = {}
        for read in self.iter_reads(chrm, strand, start, stop):
            if not read.is_read1: 
                reads_pair2[read.qname] = read
        
        # iterate through the read pairs
        for read1 in iter_pair1_reads():
            try:
                read2 = reads_pair2[ read1.qname ]
            # if there is no mate, skip this read
            except KeyError:
                if DEBUG:
                    print "No mate: ", read1.pos, read1.aend-1
                continue

            assert read1.query == None or \
                   ( read1.alen == read1.aend - read1.pos ) \
                   or ( len( read1.cigar ) > 1 )
            assert read2.query == None or \
                   ( read2.alen == read2.aend - read2.pos ) \
                   or ( len( read2.cigar ) > 1 )
            
            #if read1.qlen != read2.qlen:
            #    print( "ERROR: unequal read lengths %i and %i\n", \
            #           read1.qlen, read2.qlen )
            #    continue

            yield read1, read2

        return

    def build_read_coverage_array( self, chrm, strand, 
                                   start, stop, read_pair=None ):
        assert stop >= start
        full_region_len = stop - start + 1
        cvg = numpy.zeros(full_region_len)
        for rd in self.iter_reads( chrm, strand, start, stop ):
            if read_pair != None:
                if read_pair==1 and not rd.is_read1: continue
                if read_pair==2 and not rd.is_read2: continue
            for region in iter_coverage_regions_for_read( 
                    rd, self, self.RRR, self.PAOS):
                cvg[max(0, region[2]-start):max(0, region[3]-start)] += 1
        
        return cvg

    def reload( self ):
        fname = self.filename
        #self.close()
        reads = type(self)(fname)
        reads.init(**self._init_kwargs)
        return reads

class RNAseqReads(Reads):    
    def init(self, reverse_read_strand=None, reads_are_stranded=True, 
                   pairs_are_opp_strand=None, reads_are_paired=True,
                   ref_genes=None):        
        assert self.is_indexed()
        
        assert reads_are_paired == True, "GRIT can only use paired RNAseq reads"
        assert reads_are_stranded == True, "GRIT can only use stranded RNAseq"
        
        if pairs_are_opp_strand == None:
            pairs_are_opp_strand = (not read_pairs_are_on_same_strand( self ))
        
        if reverse_read_strand == None:
            reverse_read_strand = Reads.determine_reverse_read_strand_param(
                self, ref_genes, pairs_are_opp_strand, 'internal_exon',
                100, 10 )
            if config.VERBOSE:
                config.log_statement(
                    "Set reverse_read_strand to '%s' for '%s'" % (
                        reverse_read_strand, self.filename), log=True )

        Reads.init(self, reads_are_paired, pairs_are_opp_strand, 
                         reads_are_stranded, reverse_read_strand )
        
        self._init_kwargs = {
            'reverse_read_strand': reverse_read_strand, 
            'reads_are_stranded': reads_are_stranded, 
            'pairs_are_opp_strand': pairs_are_opp_strand, 
            'reads_are_paired': reads_are_paired
        }
        
        return self
    

class CAGEReads(Reads):
    def init(self, reverse_read_strand=None, pairs_are_opp_strand=None, 
             reads_are_paired=False, ref_genes=None ):        
        assert reverse_read_strand in ('auto', None, True, False), \
            "Invalid option for reverse read strand"
        reads_are_paired=False
        pairs_are_opp_strand = False
        assert not reads_are_paired, "GRIT can not use paired CAGE reads."

        # CAGE reads are always stranded
        reads_are_stranded = True

        if reverse_read_strand in ('auto', None):
            if ref_genes in([], None): 
                raise ValueError, "Determining reverse_read_strand requires reference genes"
            reverse_read_strand = Reads.determine_reverse_read_strand_param(
                self, ref_genes, pairs_are_opp_strand, 'tss_exon',
                100, 10 )
            if config.VERBOSE:
                config.log_statement(
                    "Set reverse_read_strand to '%s' for '%s'" % (
                        reverse_read_strand, self.filename), log=True )

        
        Reads.init(self, reads_are_paired, pairs_are_opp_strand, 
                         reads_are_stranded, reverse_read_strand )
        
        self._init_kwargs = {
            'reverse_read_strand': reverse_read_strand, 
            'pairs_are_opp_strand': pairs_are_opp_strand, 
            'reads_are_paired': reads_are_paired
        }
        
        return self
    
    def build_read_coverage_array( self, chrm, strand, start, stop, 
                                   read_pair=None ):
        assert read_pair == None
        full_region_len = stop - start + 1
        cvg = numpy.zeros(full_region_len)
        for rd in self.fetch( chrm, start, stop ):
            #assert not rd.is_paired
            if rd.mapq <= 1: continue
            rd_strand = '-' if rd.is_reverse else '+'
            if self.reverse_read_strand:
                rd_strand = '+' if rd_strand == '-' else '-'
            if strand != rd_strand: continue
            if strand == '-':
                peak_pos = max(rd.pos, rd.aend-1)
            else:
                peak_pos = min(rd.pos, rd.aend-1)
            if peak_pos < start or peak_pos > stop:
                continue
            
            cvg[peak_pos-start] += get_rd_posterior_prb(rd)
        return cvg

class RAMPAGEReads(Reads):
    def init(self, reverse_read_strand, pairs_are_opp_strand=None,
             reads_are_paired=True, ref_genes=None ):
        assert self.is_indexed()

        assert reads_are_paired, "GRIT can not use unpaired RAMPAGE reads."
        reads_are_stranded = True
        
        # reads strandedness
        if pairs_are_opp_strand == None:
            pairs_are_opp_strand = (not read_pairs_are_on_same_strand( self ))

        if reverse_read_strand in ('auto', None):
            if ref_genes in([], None): 
                raise ValueError, "Determining reverse_read_strand requires reference genes"
            reverse_read_strand = Reads.determine_reverse_read_strand_param(
                self, ref_genes, pairs_are_opp_strand, 'tss_exon',
                100, 10 )
            if config.VERBOSE:
                config.log_statement(
                    "Set reverse_read_strand to '%s' for '%s'" % (
                        reverse_read_strand, self.filename), log=True )

        
        Reads.init(self, reads_are_paired, pairs_are_opp_strand, 
                         reads_are_stranded, reverse_read_strand )
        
        self._init_kwargs = {
            'reverse_read_strand': reverse_read_strand, 
            'pairs_are_opp_strand': pairs_are_opp_strand,
            'reads_are_paired': reads_are_paired
        }
        
        return self
    
    def build_read_coverage_array( self, chrm, strand, start, stop,
                                   read_pair=None ):
        assert read_pair == None
        full_region_len = stop - start + 1
        cvg = numpy.zeros(full_region_len)
        for rd in self.fetch( chrm, start, stop ):
            #assert not rd.is_paired
            if rd.mapq <= 1: continue
            if not rd.is_read1: continue
            rd_strand = '-' if rd.is_reverse else '+'
            if self.reverse_read_strand:
                rd_strand = '+' if rd_strand == '-' else '-'            
            if strand != rd_strand: continue
            peak_pos = rd.pos
            if strand == '-':
                peak_pos = max(rd.pos, rd.aend-1)
            else:
                peak_pos = min(rd.pos, rd.aend-1)
            if peak_pos < start or peak_pos > stop:
                continue
            cvg[peak_pos-start] += get_rd_posterior_prb(rd)
        
        return cvg


class PolyAReads(Reads):
    def init(self, reverse_read_strand=None, pairs_are_opp_strand=None, 
             ref_genes=None ):
        assert self.is_indexed()

        reads_are_paired = True
        reads_are_stranded = True
        
        # reads strandedness
        if pairs_are_opp_strand == None:
            pairs_are_opp_strand = (not read_pairs_are_on_same_strand( self ))

        if reverse_read_strand in ('auto', None):
            if ref_genes in([], None): 
                raise ValueError, "Determining reverse_read_strand requires reference genes"
            reverse_read_strand = Reads.determine_reverse_read_strand_param(
                self, ref_genes, pairs_are_opp_strand, 'tes_exon',
                100, 10 )
            if config.VERBOSE:
                config.log_statement(
                    "Set reverse_read_strand to '%s' for '%s'" % (
                        reverse_read_strand, self.filename), log=True )
        
        Reads.init(self, reads_are_paired, pairs_are_opp_strand, 
                         reads_are_stranded, reverse_read_strand )
        
        self._init_kwargs = {
            'reverse_read_strand': reverse_read_strand, 
            'pairs_are_opp_strand': pairs_are_opp_strand 
        }
        
        return self
    
    def build_read_coverage_array( self, chrm, strand, start, stop,
                                   read_pair=None ):
        assert read_pair == None
        
        full_region_len = stop - start + 1
        cvg = numpy.zeros(full_region_len)
        for rd in self.fetch( chrm, start, stop ):
            # skip the read pair which doesn't contain a poly(a) site
            if rd.is_paired and rd.is_read1: continue
            
            # determine the strand of the poly(A) site
            rd_strand = self.get_strand(rd)
            
            # determine which pos of the read corresponds to the 
            # poly(a) site
            if rd_strand == '+': pos = rd.aend-1
            else: pos = rd.pos
            
            # skip sites that aren't within the requested range
            if strand != rd_strand: continue
            if pos < start or pos > stop: continue
            
            cvg[pos-start] += get_rd_posterior_prb(rd)
        
        return cvg