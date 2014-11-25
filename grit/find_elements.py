import sys, os
import time
import math
import traceback

import shutil

import numpy
from scipy.stats import beta, binom

from collections import defaultdict, namedtuple
from itertools import chain, izip
from bisect import bisect
from copy import copy, deepcopy

import multiprocessing
import Queue

import networkx as nx

ReadCounts = namedtuple('ReadCounts', ['Promoters', 'RNASeq', 'Polya'])

read_counts = None
fl_dists = None

from files.reads import MergedReads, RNAseqReads, CAGEReads, \
    RAMPAGEReads, PolyAReads, \
    fix_chrm_name_for_ucsc, get_contigs_and_lens, calc_frag_len_from_read_data, \
    iter_paired_reads
import files.junctions
from files.bed import create_bed_line
from files.gtf import parse_gtf_line, load_gtf
from files.reads import extract_jns_and_reads_in_region

from peaks import call_peaks, build_control_in_gene

from elements import find_jn_connected_exons

from frag_len import FlDist

from transcript import Transcript, Gene
import f_matrix     
import frequency_estimation
frequency_estimation.LHD_ABS_TOL = 1e-1
frequency_estimation.PARAM_ABS_TOL = 1e-3

import config

class ThreadSafeFile( file ):
    def __init__( self, *args ):
        args = list( args )
        args.insert( 0, self )
        file.__init__( *args )
        self.lock = multiprocessing.Lock()

    def write( self, line ):
        with self.lock:
            file.write( self, line )
            self.flush()

def cluster_intron_connected_segments( segments, introns ):
    if len(segments) == 0:
        return []
    segments = sorted(segments)
    segment_starts = numpy.array([x[0] for x in segments])
    segment_stops = numpy.array([x[1] for x in segments])

    edges = set()
    for start, stop in introns:
        # Skip junctions that dont fall into any segment
        if start-1 < segment_starts[0]: continue
        if stop+1 >= segment_stops[-1]: continue
        
        # find which bin the segments fall into. Note, that since the 
        # segments don't necessarily tile the genome, it's possible
        # for the returned bin to not actually contain the junction
        start_bin = segment_starts.searchsorted( start-1, side='right' )-1
        assert start_bin >= 0
        stop_bin = segment_starts.searchsorted( stop+1, side='right' )-1

        # since the read coverage is determined in part determined by 
        # the junctions, we should never see a junction that doesn't fall
        # into a segment
        try:
            assert ( segment_starts[start_bin] <= 
                     start-1 <= segment_stops[start_bin] ), str([
                         segment_starts[start_bin],
                     start-1, segment_stops[start_bin],
                         segment_starts[start_bin+1],
                         start-1, segment_stops[start_bin+1]])

            assert ( segment_starts[stop_bin] <= 
                     stop+1 <= segment_stops[stop_bin]), str([
                         segment_starts[stop_bin],
                         stop-1, segment_stops[stop_bin],
                         segment_starts[stop_bin+1],
                         stop-1, segment_stops[stop_bin+1]])
        except:
            raise
            continue
        #if start > segment_stops[start_bin]: continue
        #if stop > segment_stops[stop_bin]: continue
        # XXX - dont rememeber why I was doing this
        #assert stop_bin < len(segment_starts)-1, \
        #    str([stop_bin, len(segment_stops), segment_stops[stop_bin]])
        if start_bin != stop_bin:
            edges.add((int(min(start_bin, stop_bin)), 
                       int(max(start_bin, stop_bin))))
    
    genes_graph = nx.Graph()
    genes_graph.add_nodes_from(xrange(len(segment_starts)))
    genes_graph.add_edges_from(edges)
    
    segments = []
    for g in nx.connected_components(genes_graph):
        g = sorted(g)
        segment = []
        prev_i = g[0]
        segment.append( [segment_starts[prev_i], ])
        for i in g[1:]:
            # if we've skipped at least one node, then add
            # a new region onto this segment
            if i > prev_i + 1:
                segment[-1].append( segment_stops[prev_i] )
                segment.append( [segment_starts[i], ])
                prev_i = i
            # otherwise, we've progressed to an adjacent sergments
            # so just merge the adjacent intervals
            else:
                assert i == prev_i + 1
                prev_i += 1
        segment[-1].append( segment_stops[g[-1]] )
        
        segments.append(segment)
    
    return segments

def find_empty_regions( cov, thresh=1e-6, 
                        min_length=config.MAX_EMPTY_REGION_SIZE ):
    x = numpy.diff( numpy.asarray( cov >= thresh, dtype=int ) )
    stops = (numpy.nonzero(x==1)[0]).tolist()
    if cov[-1] < thresh: stops.append(len(x))
    starts = (numpy.nonzero(x==-1)[0] + 1).tolist()
    if cov[0] < thresh: starts.insert(0, 0)
    assert len(starts) == len(stops)
    return [ x for x in izip(starts, stops) if x[1]-x[0]+1 >= min_length ]

def find_transcribed_regions( cov, thresh=1e-6 ):
    empty_regions = find_empty_regions(cov, thresh, 0)
    if len(empty_regions) == 0: 
        return [[0, len(cov)-1],]

    transcribed_regions = []
    if empty_regions[0][0] == 0:
        transcribed_regions.append([empty_regions.pop(0)[1]+1,])
    else:
        transcribed_regions.append([0,])
    for start, stop in empty_regions:
        transcribed_regions[-1].append(start-1)
        transcribed_regions.append([stop+1,])
    if transcribed_regions[-1][0] == len(cov):
        transcribed_regions.pop()
    else:
        transcribed_regions[-1].append(len(cov)-1)

    return transcribed_regions
    
    """
    # code to try and merge low signal segments, but I think that 
    # this is the wrong appraoch. I should be merging introns that 
    # could have all come from a uniform distribution
    if len(transcribed_regions) == 0: return []
    
    # n distinct 
    seg_graph = nx.Graph()  
    seg_graph.add_nodes_from(xrange(len(transcribed_regions)))  
    y = cov[1:] - cov[:-1]
    y[y<0] = 0
    cnt_data = [ (numpy.count_nonzero(y[start:stop+1]) + 1, 
                  start, stop)
                 for start, stop in transcribed_regions]
    #if cnt_data[0][1] > 0:
    #    cnt_data.insert(0, (1, 0, cnt_data[0][1]-1))
    #if cnt_data[-1][-1] < len(cov):
    #    cnt_data.append((1, cnt_data[-1][-1]+1, len(cov)-1))
    
    for i, (nz, start, stop) in enumerate(cnt_data[1:]):
        prev_nz, p_start, p_stop = cnt_data[i]
        old_cnt = p_stop - p_start + 1
        new_cnt = stop - p_start + 1
        merged_p = float(prev_nz + nz)/(stop - p_start + 1)
        if ( start - p_stop < 10000
             and nz < binom.ppf(1-1e-6, p=merged_p, n=new_cnt)
             and prev_nz < binom.ppf(1-0.5/len(cnt_data), 
                                     p=merged_p, n=old_cnt) ):
            seg_graph.add_edge(i, i+1)

    merged_regions = []
    for regions in nx.connected_components(seg_graph):
        merged_regions.append((cnt_data[min(regions)][1], 
                               cnt_data[max(regions)][2]))
    return merged_regions
    """

def merge_adjacent_intervals(
        intervals, max_merge_distance=None):
    if len(intervals) == 0: return []
    intervals.sort()
    merged_intervals = [list(intervals[0]),]
    prev_stop = merged_intervals[-1][1]
    for start, stop in intervals[1:]:
        if start - max_merge_distance - 1 <= prev_stop:
            merged_intervals[-1][1] = stop
        else:
            merged_intervals.append([start, stop])
        prev_stop = stop
    return merged_intervals

def filter_exon(exon, wig, num_start_bases_to_skip=0, num_stop_bases_to_skip=0):
    '''Find all the exons that are sufficiently homogenous and expressed.
    
    '''
    start = exon.start + num_start_bases_to_skip
    end = exon.stop - num_stop_bases_to_skip
    if start >= end - 10: return False
    vals = wig[start:end+1]
    n_div = max( 1, int(len(vals)/config.MAX_EMPTY_REGION_SIZE) )
    div_len = len(vals)/n_div
    for i in xrange(n_div):
        seg = vals[i*div_len:(i+1)*div_len]
        if seg.mean() < config.MIN_EXON_AVG_CVG:
            return True

    return False

def filter_exons( exons, rnaseq_cov, 
                  num_start_bases_to_skip=0, 
                  num_stop_bases_to_skip=0 ):
    for exon in exons:
        if not filter_exon( exon, rnaseq_cov, 
                            num_start_bases_to_skip, 
                            num_stop_bases_to_skip ):
            yield exon
    
    return        

class Bin(object):
    start = None
    stop = None
    
    def reverse_strand(self, contig_len):
        kwargs = copy(self.__dict__)
        kwargs['start'] = contig_len-1-self.stop
        kwargs['stop'] = contig_len-1-self.start
        if 'left_labels' in kwargs:
            (kwargs['left_labels'], kwargs['right_labels'] 
             ) = (kwargs['right_labels'], kwargs['left_labels'])
        return type(self)(**kwargs)

    def mean_cov( self, cov_array ):
        return numpy.median(cov_array[self.start:self.stop+1])
        return cov_array[self.start:self.stop].mean()
    
    #def reverse_strand(self, contig_len):
    #    return Bin(contig_len-1-self.stop, contig_len-1-self.start, 
    #               self.right_label, self.left_label, self.type)

    #def reverse_coords(self, contig_len):
    #    return Bin(contig_len-1-self.stop, contig_len-1-self.start, 
    #               self.left_label, self.right_label, self.type)
        
    _bndry_color_mapping = {
        'CONTIG_BNDRY': '0,0,0',
        'GENE_BNDRY': '0,0,0',
        
        'POLYA': '255,255,0',

        'CAGE_PEAK': '0,255,0',
        
        'D_JN': '173,255,47',
        'R_JN': '0,0,255',
        
        'ESTART': '0,0,0',
        'ESTOP': '0,0,0'
    }
    
    def find_bndry_color( self, bndry ):
        return self._bndry_color_mapping[ bndry ]
    
    def _find_colors( self, strand ):
        if self.type != None:
            if self.type =='GENE':
                return '0,0,0'
            if self.type =='CAGE_PEAK':
                return '0,0,0'
            if self.type =='EXON':
                return '0,0,0'
            if self.type =='EXON_EXT':
                return '0,0,255'
            if self.type =='RETAINED_INTRON':
                return '255,255,0'
            if self.type =='TES_EXON':
                return '255,0,0'
            if self.type =='TSS_EXON':
                return '0,255,0'
            if self.type =='SE_GENE':
                return '255,255,0'
            if self.type =='INTERGENIC_SPACE':
                return '254,254,34'
        
        if strand == '+':
            left_label, right_label = self.left_label, self.right_label
        else:
            assert strand == '-'
            left_label, right_label = self.right_label, self.left_label
        
        
        if left_label == 'D_JN' and right_label  == 'R_JN':
            return '108,108,108'
        if left_label == 'D_JN' and right_label  == 'D_JN':
            return '135,206,250'
        if left_label == 'R_JN' and right_label  == 'R_JN':
            return '135,206,250'
        if left_label == 'R_JN' and right_label  == 'D_JN':
            return '0,0,255'
        if left_label == 'R_JN' and right_label  == 'POLYA':
            return '255,0,0'
        if left_label == 'POLYA' and right_label  == 'POLYA':
            return ' 240,128,128'
        if left_label == 'D_JN' and right_label  == 'POLYA':
            return '240,128,128'
        if left_label == 'POLYA' and right_label  == 'D_JN':
            return '147,112,219'
        if left_label == 'POLYA' and right_label  == 'R_JN':
            return '159,153,87'
        if left_label == 'ESTART' and right_label  == 'ESTOP':
            return '159,153,87'
        
        return ( self.find_bndry_color(left_label), 
                 self.find_bndry_color(right_label) )

class SegmentBin(Bin):
    def __init__(self, start, stop, left_labels, right_labels,
                 type=None, 
                 fpkm_lb=None, fpkm=None, fpkm_ub=None ):
        self.start = start
        self.stop = stop
        assert stop - start >= 0

        self.left_labels = sorted(left_labels)
        self.right_labels = sorted(right_labels)

        self.type = type
        
        self.fpkm_lb = fpkm_lb
        self.fpkm = fpkm
        self.fpkm_ub = fpkm_ub
        
    def __repr__( self ):
        type_str =  ( 
            "(%s-%s)" % ( ",".join(self.left_labels), 
                        ",".join(self.right_labels) ) 
            if self.type == None else self.type )
        loc_str = "%i-%i" % ( self.start, self.stop )
        rv = "%s:%s" % (type_str, loc_str)
        if self.fpkm != None:
            rv += ":%.2f-%.2f TPM" % (self.fpkm_lb, self.fpkm_ub)
        return rv

class  TranscriptElement( Bin ):
    def __init__( self, start, stop, type, fpkm):
        self.start = start
        self.stop = stop
        assert stop - start >= 0
        self.type = type
        self.fpkm = fpkm
    
    def length( self ):
        return self.stop - self.start + 1
        
    def __repr__( self ):
        type_str = self.type
        loc_str = "%i-%i" % ( self.start, self.stop )
        rv = "%s:%s" % (type_str, loc_str)
        if self.fpkm != None:
            rv += ":%.2f FPKM" % self.fpkm
        return rv

def reverse_strand(bins_iter, contig_len):
    rev_bins = []
    for bin in reversed(bins_iter):
        rev_bins.append( bin.reverse_strand( contig_len ) )
    return rev_bins

class GeneElements(object):
    def __init__( self, chrm, strand  ):
        self.chrm = chrm
        self.strand = strand
        
        self.regions = []
        self.element_segments = []
        self.elements = []

    def find_coverage(self, reads):
        cov = numpy.zeros(self.stop-self.start+1, dtype=float)
        for x in self.regions:
            seg_cov = reads.build_read_coverage_array( 
                self.chrm, self.strand, x.start, x.stop )
            cov[x.start-self.start:x.stop-self.start+1] = seg_cov
        #if gene.strand == '-': cov = cov[::-1]
        return cov
    
    def base_is_in_gene(self, pos):
        return all( r.start <= pos <= r.stop for r in self.regions )
    
    def reverse_strand( self, contig_len ):
        rev_gene = GeneElements( self.chrm, self.strand )
        for bin in reversed(self._regions):
            rev_gene._regions.append( bin.reverse_strand( contig_len ) )
        for bin in reversed(self._element_segments):
            rev_gene._element_segments.append( bin.reverse_strand( contig_len ) )
        for bin in reversed(self._exons):
            rev_gene._exons.append( bin.reverse_strand( contig_len ) )
        
        return rev_gene

    def shift( self, shift_amnt ):
        rev_gene = GeneElements( self.chrm, self.strand )
        for bin in self._regions:
            rev_gene._regions.append( bin.shift( shift_amnt ) )
        for bin in self._element_segments:
            rev_gene._element_segments.append( bin.shift( shift_amnt ) )
        for bin in self._exons:
            rev_gene._exons.append( bin.shift( shift_amnt ) )
        
        return rev_gene
        
    @property
    def start(self):
        return min( x.start for x in self.regions )

    @property
    def stop(self):
        return max( x.stop for x in self.regions )

    def write_elements_bed( self, ofp ):
        feature_mapping = { 
            'GENE': 'gene',
            'CAGE_PEAK': 'promoter',
            'SE_GENE': 'single_exon_gene',
            'TSS_EXON': 'tss_exon',
            'EXON': 'internal_exon',
            'TES_EXON': 'tes_exon',
            'INTRON': 'intron',
            'POLYA': 'polya',
            'INTERGENIC_SPACE': 'intergenic',
            'RETAINED_INTRON': 'retained_intron',
            'UNKNOWN': 'UNKNOWN'
        }

        color_mapping = { 
            'GENE': '200,200,200',
            'CAGE_PEAK': '153,255,000',
            'SE_GENE': '000,000,200',
            'TSS_EXON': '140,195,59',
            'EXON': '000,000,000',
            'TES_EXON': '255,51,255',
            'INTRON': '100,100,100',
            'POLYA': '255,0,0',
            'INTERGENIC_SPACE': '254,254,34',
            'RETAINED_INTRON': '255,255,153',
            'UNKNOWN': '0,0,0'
        }

        chrm = self.chrm
        if config.FIX_CHRM_NAMES_FOR_UCSC:
            chrm = fix_chrm_name_for_ucsc(chrm)

        # write the gene line
        bed_line = create_bed_line( chrm, self.strand, 
                                    self.start, self.stop+1, 
                                    feature_mapping['GENE'],
                                    score=1000,
                                    color=color_mapping['GENE'],
                                    use_thick_lines=True,
                                    blocks=[(x.start, x.stop) for x in self.regions])
        ofp.write( bed_line + "\n"  )

        try: max_min_fpkm = max( x.fpkm_lb for x in self.element_segments )
        except: max_min_fpkm = 1000
        for element in self.elements:
            region = ( chrm, self.strand, element.start, element.stop)

            blocks = []
            use_thick_lines=(element.type != 'INTRON')
            element_type = element.type
            if element_type == None: element_type = 'UNKNOWN'
            try: fpkm = element.fpkm_ub
            except: fpkm = element.fpkm
            score = int(1000*fpkm/max_min_fpkm)

            grp_id = element_type + "_%s_%s_%i_%i" % region

            # also, add 1 to stop because beds are open-closed ( which means no net 
            # change for the stop coordinate )
            bed_line = create_bed_line( chrm, self.strand, 
                                        element.start, element.stop+1, 
                                        feature_mapping[element_type],
                                        score=score,
                                        color=color_mapping[element_type],
                                        use_thick_lines=use_thick_lines,
                                        blocks=blocks)
            ofp.write( bed_line + "\n"  )

        return

    def writeGff( self, ofp ):
        """
            chr7    127471196  127472363  Pos1  0  +  127471196  127472363  255,0,0
        """
        if self.strand == '-':
            writetable_bins = self.reverse_strand( contig_len )
        else:
            writetable_bins = self
        
        for bin in writetable_bins:
            if filter != None and bin.type != filter:
                continue
            chrm = elements.chrm
            if config.FIX_CHRM_NAMES_FOR_UCSC:
                chrm = fix_chrm_name_for_ucsc(self.chrm)
            # add 1 because gffs are 1-based
            region = GenomicInterval(chrm, self.strand, 
                                     bin.start+1, bin.stop+1)
            grp_id = "%s_%s_%i_%i" % region
            ofp.write( create_gff_line(region, grp_id) + "\n" )
        
        return

def find_cage_peak_bins_in_gene( gene, cage_reads, rnaseq_reads ):
    rnaseq_cov = gene.find_coverage( rnaseq_reads )
    print rnaseq_cov
    cage_cov = gene.find_coverage( cage_reads)
    print cage_cov
    assert False
    # threshold the CAGE data. We assume that the CAGE data is a mixture of 
    # reads taken from actually capped transcripts, and random transcribed 
    # regions, or RNA seq covered regions. We zero out any bases where we
    # can't reject the null hypothesis that the observed CAGE reads all derive 
    # from the background, at alpha = 0.001. 
    rnaseq_cov = numpy.array( rnaseq_cov+1-1e-6, dtype=int)
    max_val = rnaseq_cov.max()
    thresholds = config.TOTAL_MAPPED_READS*beta.ppf( 
        config.CAGE_FILTER_ALPHA, 
        numpy.arange(max_val+1)+1, 
        numpy.zeros(max_val+1)+(config.TOTAL_MAPPED_READS+1) 
    )
    max_scores = thresholds[ rnaseq_cov ]
    cage_cov[ cage_cov < max_scores ] = 0    
    
    raw_peaks = find_peaks( cage_cov, window_len=config.CAGE_PEAK_WIN_SIZE, 
                            min_score=config.MIN_NUM_CAGE_TAGS,
                            max_score_frac=config.MAX_CAGE_FRAC,
                            max_num_peaks=100)
    
    cage_peak_bins = [] #Bins( gene.chrm, gene.strand )
    for pk_start, pk_stop in raw_peaks:
        cnt = float(cage_cov[pk_start:pk_stop+1].sum())
        bin = SegmentBin(
            gene.start+pk_start, gene.start+pk_stop, 
            "CAGE_PEAK_START", "CAGE_PEAK_STOP", "CAGE_PEAK",
            1e6*beta.ppf(0.01, cnt+1e-6, read_counts.Promoters+1e-6),
            1e6*beta.ppf(0.50, cnt+1e-6, read_counts.Promoters+1e-6),
            1e6*beta.ppf(0.99, cnt+1e-6, read_counts.Promoters+1e-6) )
        cage_peak_bins.append(bin)
    
    return cage_peak_bins

def find_polya_peak_bins_in_gene( gene, polya_reads, rnaseq_reads ):
    polya_cov = gene.find_coverage(polya_reads)
    
    # threshold the polya data. We assume that the polya data is a mixture of 
    # reads taken from actually capped transcripts, and random transcribed 
    # regions, or RNA seq covered regions. We zero out any bases where we
    # can't reject the null hypothesis that the observed polya reads all derive 
    # from the background, at alpha = 0.001. 
    """
    rnaseq_cov = find_coverage_in_gene( gene, rnaseq_reads )
    rnaseq_cov = numpy.array( rnaseq_cov+1-1e-6, dtype=int)
    max_val = rnaseq_cov.max()
    thresholds = TOTAL_MAPPED_READS*beta.ppf( 
        0.1, 
        numpy.arange(max_val+1)+1, 
        numpy.zeros(max_val+1)+(TOTAL_MAPPED_READS+1) 
    )
    max_scores = thresholds[ rnaseq_cov ]
    polya_cov[ polya_cov < max_scores ] = 0    
    """
    
    raw_peaks = find_peaks( polya_cov, window_len=30, 
                            min_score=config.MIN_NUM_POLYA_TAGS,
                            max_score_frac=0.05,
                            max_num_peaks=100)
    polya_sites = [] #Bins( gene.chrm, gene.strand )
    if len( raw_peaks ) == 0:
        return polya_sites
    
    for pk_start, pk_stop in raw_peaks:
        cnt = float(polya_cov[pk_start:pk_stop+1].sum())
        bin = SegmentBin(
            gene.start+pk_start, gene.start+pk_stop, 
            "POLYA_PEAK_START", "POLYA_PEAK_STOP", "POLYA",
            1e6*beta.ppf(0.01, cnt+1e-6, read_counts.Polya+1e-6),
            1e6*beta.ppf(0.50, cnt+1e-6, read_counts.Polya+1e-6),
            1e6*beta.ppf(0.99, cnt+1e-6, read_counts.Polya+1e-6) )
        polya_sites.append(bin)
    
    return polya_sites

def find_peaks( cov, window_len, min_score, max_score_frac, max_num_peaks ):    
    def overlaps_prev_peak( new_loc ):
        for start, stop in peaks:
            if not( new_loc > stop or new_loc + window_len < start ):
                return True
        return False
    
    # merge the peaks
    def grow_peak( start, stop, grow_size=
                   max(1, window_len/4), min_grow_ratio=config.MAX_CAGE_FRAC ):
        # grow a peak at most max_num_peaks times
        max_mean_signal = cov[start:stop+1].mean()
        for i in xrange(max_num_peaks):
            curr_signal = cov[start:stop+1].sum()
            if curr_signal < min_score:
                return ( start, stop )
            
            downstream_sig = float(cov[max(0, start-grow_size):start].sum())/grow_size
            upstream_sig = float(cov[stop+1:stop+1+grow_size].sum())/grow_size
            
            # if neither passes the threshold, then return the current peak
            if max(upstream_sig, downstream_sig) \
                    < min_grow_ratio*curr_signal/float(stop-start+1): 
                return (start, stop)
            
            # if the expansion isn't greater than the min ratio, then return
            if max(upstream_sig,downstream_sig) < \
                    config.MAX_CAGE_FRAC*max_mean_signal:
                return (start, stop)
            
            # otherwise, we know one does
            if upstream_sig > downstream_sig:
                stop += grow_size
            else:
                start = max(0, start - grow_size )
        
        if config.VERBOSE:
            config.log_statement( 
                "Warning: reached max peak iteration at %i-%i ( signal %.2f )"
                    % (start, stop, cov[start:stop+1].sum() ) )
        return (start, stop )
    
    peaks = []
    peak_scores = []
    cumsum_cvg_array = (
        numpy.append(0, numpy.cumsum( cov )) )
    scores = cumsum_cvg_array[window_len:] - cumsum_cvg_array[:-window_len]
    indices = numpy.argsort( scores )
    min_score = max( min_score, config.MAX_CAGE_FRAC*scores[ indices[-1] ] )
    for index in reversed(indices):
        if not overlaps_prev_peak( index ):
            score = scores[ index ]
            new_peak = grow_peak( index, index + window_len )
            # if we are below the minimum score, then we are done
            if score < min_score:
                break
            
            # if we have observed peaks, and the ratio between the highest
            # and the lowest is sufficeintly high, we are done
            if len( peak_scores ) > 0:
                if float(score)/peak_scores[0] < max_score_frac:
                    break
                        
            peaks.append( new_peak ) 
            peak_scores.append( score )
    
    if len( peaks ) == 0:
        return []
    
    # merge cage peaks together
    def merge_peaks( peaks_and_scores ):
        peaks_and_scores = sorted( list(x) for x in peaks_and_scores )
        peak, score = peaks_and_scores.pop()
        new_peaks = [peak,]
        new_scores = [score,]
        while len(peaks_and_scores) >  0:
            last_peak = new_peaks[-1]
            peak, score = peaks_and_scores.pop()
            new_peak = (min(peak[0], last_peak[0]), max(peak[1], last_peak[1]))
            if (new_peak[1] - new_peak[0]) <= 1.5*( 
                    last_peak[1] - last_peak[0] + peak[1] - peak[0] ):
                new_peaks[-1] = new_peak
                new_scores[-1] += score
            else:
                new_peaks.append( peak )
                new_scores.append( score )
        
        return zip( new_peaks, new_scores )
    
    peaks_and_scores = sorted( zip(peaks, peak_scores) )
    
    for i in xrange( 99 ):
        if i == 100: assert False
        old_len = len( peaks_and_scores )
        peaks_and_scores = merge_peaks( peaks_and_scores )
        if len( peaks_and_scores ) == old_len: break
    
        
    new_peaks_and_scores = []
    for peak, score in peaks_and_scores:
        peak_scores = cov[peak[0]:peak[1]+1]
        max_score = peak_scores.max()
        good_indices = (peak_scores >= max_score*config.MAX_CAGE_FRAC).nonzero()[0]
        new_peak = [
                peak[0] + int(good_indices.min()), 
                peak[0] + int(good_indices.max())  ]
        new_score = float(cov[new_peak[0]:new_peak[1]+1].sum())
        new_peaks_and_scores.append( (new_peak, new_score) )
    
    peaks_and_scores = sorted( new_peaks_and_scores )
    max_score = max( s for p, s in peaks_and_scores )
    return [ pk for pk, score in peaks_and_scores \
                 if score >= config.MAX_CAGE_FRAC*max_score
                 and score > min_score ]

def estimate_peak_expression(peaks, peak_cov, rnaseq_cov, peak_type):
    assert peak_type in ('TSS', 'TES')
    total_read_cnts = ( 
        read_counts.Promoters if peak_type == 'TSS' else read_counts.Polya )
    strand = '+' if peak_type == 'TSS' else '-'
    for peak in peaks:
        cnt = float(peak_cov[peak.start:peak.stop+1].sum())
        peak.tpm = cnt/total_read_cnts
        peak.tpm_ub = 1e6*beta.ppf(0.99, cnt+1, total_read_cnts)/total_read_cnts
        peak.tpm_lb = 1e6*beta.ppf(0.01, cnt+1, total_read_cnts)/total_read_cnts
    
    return peaks

def iter_retained_intron_connected_exons(
        left_exons, right_exons, retained_introns,
        max_num_exons=None):
    graph = nx.DiGraph()
    left_exons = sorted((x.start, x.stop) for x in left_exons)
    right_exons = sorted((x.start, x.stop) for x in right_exons)

    graph.add_nodes_from(chain(left_exons, right_exons))
    retained_jns = [ (x.start+1, x.stop-1) for x in retained_introns ]
    edges = find_jn_connected_exons(set(chain(left_exons, right_exons)), 
                                    retained_jns, '+')
    graph.add_edges_from( (start, stop) for jn, start, stop in edges )    
    cntr = 0
    for left in left_exons:
        for right in right_exons:
            if left[1] > right[0]: continue
            for x in nx.all_simple_paths(
                    graph, left, right, max_num_exons-cntr+1):
                cntr += 1
                if max_num_exons != None and cntr > max_num_exons:
                    raise ValueError, "Too many retained introns"
                yield (x[0][0], x[-1][1])
    return

def merge_tss_exons(tss_exons):
    grpd_exons = defaultdict(list)
    for exon in tss_exons:
        grpd_exons[exon.stop].append(exon)
    merged_tss_exons = []
    for stop, exons in grpd_exons.iteritems():
        exons.sort(key=lambda x:x.start)
        curr_start = exons[0].start
        score = exons[0].score
        for exon in exons[1:]:
            if exon.start - curr_start < config.TSS_EXON_MERGE_DISTANCE:
                score += exon.score
            else:
                merged_tss_exons.append( Bin(curr_start, stop,
                                             exon.left_label, 
                                             exon.right_label,
                                             exon.type, score) )
                curr_start = exon.start
                score = exon.score
            
        merged_tss_exons.append( Bin(curr_start, stop,
                                     exon.left_label, 
                                     exon.right_label,
                                     exon.type, score) )
    return merged_tss_exons

def merge_tes_exons(tes_exons):
    grpd_exons = defaultdict(list)
    for exon in tes_exons:
        grpd_exons[exon.start].append(exon)
    merged_tes_exons = []
    for start, exons in grpd_exons.iteritems():
        exons.sort(key=lambda x:x.stop)
        curr_stop = exons[0].stop
        score = exons[0].score
        for exon in exons[1:]:
            if exon.stop - curr_stop < config.TES_EXON_MERGE_DISTANCE:
                curr_stop = exon.stop
                score += exon.score
            else:
                merged_tes_exons.append( Bin(start, curr_stop,
                                            exon.left_label, 
                                            exon.right_label,
                                            exon.type, score) )
                curr_stop = exon.stop
                score = exon.score

        merged_tes_exons.append( Bin(start, curr_stop,
                                     exon.left_label, 
                                     exon.right_label,
                                     exon.type, score) )
    return merged_tes_exons


def find_transcribed_fragments_covering_region(
        segment_graph, segment_bnds, segment_bnd_labels, 
        segment_index, max_frag_len, use_genome_coords=False):
    # first find transcripts before this segment
    def seg_len(i): 
        return segment_bnds[i+1]-segment_bnds[i]
    
    def build_neighbor_paths(side):
        assert side in ('BEFORE', 'AFTER')
        complete_paths = []
        partial_paths = [([segment_index,], 0),]
        while len(partial_paths) > 0:
            curr_path, curr_path_len = partial_paths.pop()
            if side == 'BEFORE':
                neighbors = list(segment_graph.predecessors(curr_path[0]))
            else:
                neighbors = list(segment_graph.successors(curr_path[-1]))
            
            if len(neighbors) == 0: 
                complete_paths.append((curr_path, curr_path_len))
            else:
                for child in neighbors:                    
                    if side == 'BEFORE':
                        new_path = [child,] + curr_path
                    else:
                        new_path = curr_path + [child,]

                    # if we have hit a tss then add this complete path
                    if segment_bnd_labels[segment_bnds[child]] in ('TSS','TES'):
                        complete_paths.append(new_path)
                    
                    new_path_len = seg_len(child) + curr_path_len
                    if new_path_len >= max_frag_len:
                        complete_paths.append((new_path, new_path_len))
                    else:
                        partial_paths.append((new_path, new_path_len))
        if side == 'BEFORE': return [x[:-1] for x, x_len in complete_paths]
        else: return [x[1:] for x, x_len in complete_paths]
    
    def build_genome_segments_from_path(segment_indexes):        
        merged_intervals = merge_adjacent_intervals(
            zip(segment_indexes, segment_indexes), 
            max_merge_distance=0)
        coords = []
        for start, stop in merged_intervals:
            coords.append([segment_bnds[start], segment_bnds[stop+1]-1])
        return coords
        
    complete_before_paths = build_neighbor_paths('BEFORE')
    complete_after_paths = build_neighbor_paths('AFTER')    
    transcripts = []
    for bp in complete_before_paths:
        for ap in complete_after_paths:
            segments = bp + [segment_index,] + ap
            if use_genome_coords:
                before_trim_len = max(0, sum(seg_len(i) for i in bp) - max_frag_len)
                after_trim_len = max(0, sum(seg_len(i) for i in ap) - max_frag_len)
                segments = build_genome_segments_from_path(segments)
                segments[0][0] = segments[0][0] + before_trim_len
                segments[-1][1] = segments[-1][1] - after_trim_len
            transcripts.append( segments )
    return transcripts


def extract_jns_and_paired_reads_in_gene(gene, reads):
    pair1_reads = defaultdict(list)
    pair2_reads = defaultdict(list)
    plus_jns = defaultdict(int)
    minus_jns = defaultdict(int)
    
    for region in gene.regions:
        ( r_pair1_reads, r_pair2_reads, r_plus_jns, r_minus_jns, 
          ) = extract_jns_and_reads_in_region(
            (gene.chrm, gene.strand, region.start, region.stop), reads)
        for jn, cnt in r_plus_jns.iteritems(): 
            plus_jns[jn] += cnt 
        for jn, cnt in r_minus_jns.iteritems(): 
            minus_jns[jn] += cnt 
        for qname, read_mappings in r_pair1_reads.iteritems(): 
            pair1_reads[qname].extend(read_mappings)
        for qname, read_mappings in r_pair2_reads.iteritems(): 
            pair2_reads[qname].extend(read_mappings)

    paired_reads = list(iter_paired_reads(pair1_reads, pair2_reads))
    jns, opp_strand_jns = (
        (plus_jns, minus_jns) if gene.strand == '+' else (minus_jns, plus_jns)) 
    return paired_reads, jns, opp_strand_jns

def find_exon_segments_and_introns_in_gene( 
        gene, 
        rnaseq_reads, tss_reads, tes_reads, ref_elements ):
    assert isinstance( gene, GeneElements )
    config.log_statement( 
        "Extracting reads and jns in Chrm %s Strand %s Pos %i-%i" %
        (gene.chrm, gene.strand, gene.start, gene.stop) )
    
    # initialize the cage peaks with the reference provided set
    tss_bins = [ Bin(pk_start, pk_stop, 
                     "CAGE_PEAK_START", "CAGE_PEAK_STOP", "CAGE_PEAK")
                 for pk_start, pk_stop in ref_elements['promoter'] ]
    
    # initialize the polya peaks with the reference provided set
    tes_bins = [ Bin( pk_start, pk_stop, 
                      "POLYA_PEAK_START", "POLYA_PEAK_STOP", "POLYA")
                 for pk_start, pk_stop in ref_elements['polya'] ]
    
    # build and pair rnaseq reads, and extract junctions
    paired_rnaseq_reads, jns, opp_strand_jns = extract_jns_and_paired_reads_in_gene(
        gene, rnaseq_reads)
    jns = filter_jns(jns, opp_strand_jns, set(ref_elements['introns']))
    # add in connectivity junctions
    for distal_reads in (tss_reads, tes_reads):
        if distal_reads == None: continue
        for jn, cnt, entropy in files.junctions.load_junctions_in_bam(
              distal_reads, 
              [ (gene.chrm, gene.strand, r.start, r.stop) for r in gene.regions]
              )[(gene.chrm, gene.strand)]:
            jns[jn] += 0
    
    # add in reference junctions
    for jn in ref_elements['introns']: jns[jn] += 0
        
    config.log_statement( 
        "Building exon segments in Chrm %s Strand %s Pos %i-%i" %
        (gene.chrm, gene.strand, gene.start, gene.stop) )
    
    # build the pseudo exon set
    segment_bnds = set([gene.start, gene.stop+1])
    segment_bnd_labels = defaultdict(set)
    segment_bnd_labels[gene.start].add("GENE_BNDRY")
    segment_bnd_labels[gene.stop+1].add("GENE_BNDRY")
    # add in empty regions - we will use these to filter bins
    # that fall outside of the gene
    for r1, r2 in izip(gene.regions[:-1], gene.regions[1:]):
        assert r1.stop+2 < r2.start-1+2
        segment_bnds.add(r1.stop+1)
        segment_bnd_labels[r1.stop+1].add( 'EMPTY_START' )
        segment_bnds.add(r2.start+1)
        segment_bnd_labels[r2.start+1].add( 'EMPTY_STOP' )
    
    for (start, stop) in jns:
        segment_bnds.add(start)
        segment_bnd_labels[start].add('D_JN' if gene.strand == '+' else 'R_JN')
        segment_bnds.add(stop+1)
        segment_bnd_labels[stop+1].add('R_JN' if gene.strand == '+' else 'D_JN')
    
    if tss_reads != None:
        control_cov = build_control_in_gene(
            gene, paired_rnaseq_reads, sorted(segment_bnds), 
            '5p' if gene.strand == '+' else '3p')
        signal_cov = gene.find_coverage( tss_reads )
        tss_peaks = call_peaks( signal_cov, control_cov,
                                '5p' if gene.strand == '+' else '3p')
        print tss_peaks
        assert False
    
    if tes_reads != None:
        tes_bins.extend( 
            find_polya_peak_bins_in_gene( 
                gene, polya_reads, rnaseq_reads ) )

    for tss_bin in tss_regions:
        tss_start = tss_bin.start if gene.strand == '+' else tss_bin.stop + 1
        segment_bnds.add(tss_start)
        segment_bnd_labels[tss_start].add('TSS')

    for tes_bin in tes_regions:
        tes_start = tes_bin.stop + 1 if gene.strand == '+' else tes_bin.start
        segment_bnds.add(tes_start)
        segment_bnd_labels[tes_start].add('TES')
    
    # remove boundaries that fall inside of empty regions
    empty_segments = set()
    in_empty_region = False
    for index, bnd in enumerate(sorted(segment_bnds)):
        if in_empty_region:
            assert 'EMPTY_STOP' in segment_bnd_labels[bnd]
        empty_start = bool('EMPTY_START' in segment_bnd_labels[bnd])
        empty_stop = bool('EMPTY_STOP' in segment_bnd_labels[bnd])
        assert not (empty_start and empty_stop)
        if empty_start: in_empty_region = True
        if empty_stop: in_empty_region = False
        if in_empty_region:
            empty_segments.add(index)
    
    segment_bnds = numpy.array(sorted(segment_bnds))
    # build the exon segment connectivity graph
    splice_graph = nx.DiGraph()
    splice_graph.add_nodes_from(xrange(0, len(segment_bnds)-1))
    for i in xrange(len(segment_bnds)-1-1):
        if i not in empty_segments and i+1 not in empty_segments:
            splice_graph.add_edge(i, i+1)
    
    jn_edges = []
    for (start, stop) in jns:
        start_i = segment_bnds.searchsorted(start)-1
        assert segment_bnds[start_i+1] == start
        stop_i = segment_bnds.searchsorted(stop+1)-1+1
        assert segment_bnds[stop_i] == stop+1
        jn_edges.append((start_i, stop_i))
        splice_graph.add_edge(start_i, stop_i)
    
    # fidn the marginal read length/ fragmnet length distribution
    read_groups_and_rls = defaultdict(float)
    for rd_key, mappings in paired_rnaseq_reads:
        for mapping in mappings:
            RG = mapping[2]
            # we assume all of the read lengths are identical
            rd_len = mapping[4].read_len
            read_groups_and_rls[(RG, rd_len)] += mapping[-1]
    read_groups_and_rls = dict(read_groups_and_rls)
    total_cnt = sum(read_groups_and_rls.values())
    for key, val in list(read_groups_and_rls.iteritems()):
        read_groups_and_rls[key] = val/float(total_cnt)
    max_fl = max(fl_dist.fl_max for fl_dist in fl_dists.values())
    fl_dists_and_read_lens = [
        (RG, fl_dists[RG], read_len) for RG, read_len in read_groups_and_rls]
    
    # find all possible transcripts, and their association with each element
    exon_lens = segment_bnds[1:] - segment_bnds[:-1]
    all_transcripts = set()
    segment_transcripts_map = {}
    for segment_index in splice_graph.nodes():
        transcripts = find_transcribed_fragments_covering_region(
            splice_graph, segment_bnds, segment_bnd_labels, segment_index, max_fl)
        segment_transcripts_map[(segment_index,)] = transcripts
        all_transcripts.update(tuple(t) for t in transcripts)
    
    def bin_contains_element(bin, element):
        try: 
            si = bin.index(element[0])
            if tuple(bin[si:si+len(element)]) == element: 
                return True
        except ValueError: 
            return False
        return False
    
    # add in the splice elements
    for splice in jn_edges:
        # find transcripts that contain this splice
        segment_transcripts_map[splice] = [
            t for t in segment_transcripts_map[(splice[0],)] 
            if bin_contains_element(t, splice)]
    
    # pre-calculate the expected and observed counts
    binned_reads = f_matrix.bin_rnaseq_reads( 
        rnaseq_reads, gene.chrm, gene.strand, segment_bnds, include_read_type=False)
    expected_cnts = f_matrix.calc_expected_cnts(
        segment_bnds, all_transcripts, fl_dists_and_read_lens)
    
    segment_bins, jn_bins = [], []
    for j, (element, transcripts) in enumerate(segment_transcripts_map.iteritems()):
        if config.VERBOSE:
            config.log_statement( 
                "Estimating element expression for segment %i/%i in Chrm %s Strand %s Pos %i-%i" %
                (j, len(segment_transcripts_map), 
                 gene.chrm, gene.strand, gene.start, gene.stop) )

        # find the normalized, expected counts for this element
        exp_bin_cnts_in_segment = defaultdict(
            lambda: numpy.zeros(len(transcripts), dtype=float))
        effective_t_lens = numpy.zeros(len(transcripts), dtype=float)
        for i, transcript in enumerate(transcripts):
            for (rl, RG, pe_bin), frag_cnts in expected_cnts[
                    tuple(transcript)].iteritems():
                # skip bins that don't overlap the desired element
                if not any(bin_contains_element(bin, element) for bin in pe_bin): 
                    continue
                weighted_num_distinct_frags = read_groups_and_rls[
                    (RG, rl)]*float(frag_cnts)
                effective_t_lens[i] += weighted_num_distinct_frags
                exp_bin_cnts_in_segment[pe_bin][i] += weighted_num_distinct_frags
        exp_a, obs_a, zero = f_matrix.build_expected_and_observed_arrays(
            exp_bin_cnts_in_segment, binned_reads)
        exp_a, obs_a = f_matrix.cluster_rows(exp_a, obs_a)
        try: t_freqs = frequency_estimation.estimate_transcript_frequencies(obs_a, exp_a)
        except frequency_estimation.TooFewReadsError: 
            t_freqs = numpy.ones(len(transcripts), dtype=float)/len(transcripts)
        cnt_frac_lb = beta.ppf(
            0.01, obs_a.sum()+1e-6, read_counts.RNASeq-obs_a.sum()+1e-6)
        # we use a beta to make sure that this lies between the bounds
        cnt_frac_mle = beta.ppf(
            0.50, obs_a.sum()+1e-6, read_counts.RNASeq-obs_a.sum()+1e-6)
        cnt_frac_ub = beta.ppf(
            0.99, obs_a.sum()+1e-6, read_counts.RNASeq-obs_a.sum()+1e-6)
        fpkm_lb, fpkm, fpkm_ub = 1e6*numpy.array(
            [cnt_frac_lb, cnt_frac_mle, cnt_frac_ub])*(
            t_freqs/(effective_t_lens/1000.)).sum()
        if len(element) == 1:
            start, stop = segment_bnds[element[0]], segment_bnds[element[0]+1]-1
            left_labels = segment_bnd_labels[start]
            right_labels = segment_bnd_labels[stop+1]
            bin = SegmentBin( start, stop, left_labels, right_labels,
                              type=None,
                              fpkm_lb=fpkm_lb, fpkm=fpkm, fpkm_ub=fpkm_ub )
            segment_bins.append(bin)
        else:
            start, stop = segment_bnds[element[0]+1], segment_bnds[element[1]]-1
            bin = SegmentBin( start, stop,
                              'D_JN' if gene.strand == '+' else 'R_JN',
                              'R_JN' if gene.strand == '-' else 'D_JN',
                              type='INTRON',
                              fpkm_lb=fpkm_lb, fpkm=fpkm, fpkm_ub=fpkm_ub )
            jn_bins.append(bin)

    segment_bins.sort(key=lambda x:x.start)
    return segment_bins, jn_bins

def determine_exon_type(left_label, right_label):
    if left_label == 'TSS':
        if right_label == 'TES': 
            return 'SE_GENE'
        else:
            assert right_label == 'D_JN'
            return 'TSS_EXON'

    if right_label == 'TES':
        # we should have alrady caught this case
        assert left_label != 'TES'
        assert left_label == 'R_JN'
        return 'TES_EXON'

    assert left_label == 'R_JN' and right_label == 'D_JN'
    return 'EXON'

def build_exons_from_exon_segments(gene, exon_segments, max_min_expression):
    if gene.strand == '-':
        exon_segments = reverse_strand(
            sorted(exon_segments, key=lambda x:x.start), gene.stop)
    
    EXON_START_LABELS = ('TSS', 'R_JN')
    EXON_STOP_LABELS = ('TES', 'D_JN')
    
    # find all of the exon start bins
    exons = [] #Bins(exon_segments.chrm, exon_segments.strand)
    for i in xrange(len(exon_segments)):
        # if this is not allowed to be the start of an exon
        start_segment = exon_segments[i]
        for start_label in start_segment.left_labels:
            if start_label not in EXON_START_LABELS:
                continue
            
            for j in xrange(i, len(exon_segments)):
                stop_segment = exon_segments[j]
                if stop_segment.fpkm_ub < max_min_expression: 
                    break
                
                for stop_label in stop_segment.right_labels:
                    print start_label, stop_label
                    if stop_label not in EXON_STOP_LABELS:
                        continue
                    fpkm = min(segment.fpkm for segment in exon_segments[i:j+1])
                    exon_bin = TranscriptElement(
                        start_segment.start, stop_segment.stop, 
                        determine_exon_type(start_label, stop_label),
                        fpkm)
                    exons.append(exon_bin)
    if gene.strand == '-':
        exons = reverse_strand(exons, gene.stop)
    
    return exons

def find_exons_in_gene( gene, contig_lens, ofp,
                        ref_elements, ref_elements_to_include,
                        rnaseq_reads, cage_reads, polya_reads):
    # extract the reference elements that we want to add in
    gene_ref_elements = defaultdict(list)
    for key, vals in ref_elements[(gene.chrm, gene.strand)].iteritems():
        if len( vals ) == 0: continue
        for start, stop in sorted(vals):
            if stop < gene.start: continue
            if start > gene.stop: break
            if gene.base_is_in_gene(start) and gene.base_is_in_gene(stop):
                gene_ref_elements[key].append((start, stop))

    config.log_statement( "Finding Exons in Chrm %s Strand %s Pos %i-%i" %
                   (gene.chrm, gene.strand, gene.start, gene.stop) )
    
    # gene_ref_elements['intron']
    exon_segments, introns, tss_bins, tes_bins = find_exon_segments_and_introns_in_gene(
        gene, 
        rnaseq_reads, cage_reads, polya_reads, 
        ref_elements )
    gene.element_segments.extend(chain(introns, exon_segments, tss_bins, tes_bins))
    
    # build exons, and add them to the gene
    min_max_exon_expression = max(
        config.MIN_EXON_FPKM, 
        max(x.fpkm_lb for x in chain(exon_segments, introns))/config.MAX_EXPRESISON_RATIO)
    gene.elements.extend(build_exons_from_exon_segments(
            gene, exon_segments, min_max_exon_expression))
    
    # introns are both elements and element segments
    gene.elements.extend(
        intron for intron in introns if intron.fpkm_ub > min_max_exon_expression)

    # merge in the reference elements
    for tss_exon in gene_ref_elements['tss_exon']:
        gene.elements.append( Bin(tss_exon[0], tss_exon[1], 
                                  "REF_TSS_EXON_START", "REF_TSS_EXON_STOP",
                                  "TSS_EXON") )
    for tes_exon in gene_ref_elements['tes_exon']:
        gene.elements.append( Bin(tes_exon[0], tes_exon[1], 
                                  "REF_TES_EXON_START", "REF_TES_EXON_STOP",
                                  "TES_EXON") )
    
    # add the gene bin
    gene.write_elements_bed(ofp)
    
    config.log_statement( "FINISHED Finding Exons in Chrm %s Strand %s Pos %i-%i" %
                   (gene.chrm, gene.strand, gene.start, gene.stop) )
    return None

def find_exons_worker( (genes_queue, genes_queue_lock, n_threads_running), 
                       ofp, contig_lens, ref_elements, ref_elements_to_include,
                       rnaseq_reads, cage_reads, polya_reads ):
        
    rnaseq_reads = rnaseq_reads.reload()
    cage_reads = cage_reads.reload() if cage_reads != None else None
    polya_reads = polya_reads.reload() if polya_reads != None else None
    
    while True:
        # try to get a gene
        with genes_queue_lock:
            try: gene = genes_queue.pop()
            except IndexError: gene = None
        
        # if there is no gene it process, but threads are still running, then 
        # wait for the queue to fill or the process to finish
        if gene == None and n_threads_running.value > 0:
            config.log_statement( 
                "Waiting for gene to process (%i)" % n_threads_running.value)
            time.sleep(0.1)
            continue
        # otherwise, take a lock to make sure that the queue is empty and no 
        # threads are running. If so, then return
        elif gene == None:
            with genes_queue_lock:
                if len(genes_queue) == 0 and n_threads_running.value == 0:
                    config.log_statement( "" )
                    return
                else: continue

        # otherwise, we have a gene to process, so process it
        assert gene != None
        with genes_queue_lock: n_threads_running.value += 1

        # find the exons and genes
        try:
            rv = find_exons_in_gene(gene, contig_lens, ofp,
                                    ref_elements, ref_elements_to_include,
                                    rnaseq_reads, cage_reads, polya_reads)
        except Exception, inst:
            config.log_statement( 
                "Uncaught exception in find_exons_in_gene", log=True )
            config.log_statement( traceback.format_exc(), log=True, display=False )
            rv = None
        
        # if the return value is new genes, then add these to the queue
        if rv != None:
            with genes_queue_lock:
                for gene in rv:
                    genes_queue.append( gene )
                n_threads_running.value -= 1
        # otherwise, we found the exons and wrote out the element data,
        # so just decrease the number of running threads
        else:
            with genes_queue_lock:
                n_threads_running.value -= 1
    
    assert False
    return

def extract_reference_elements(genes, ref_elements_to_include):
    ref_elements = defaultdict( lambda: defaultdict(set) )
    if not any(ref_elements_to_include):
        return ref_elements
    
    for gene in genes:
        elements = gene.extract_elements()
        def add_elements(key):
            for start, stop in elements[key]:
                ref_elements[(gene.chrm, gene.strand)][key].add((start, stop))

        if ref_elements_to_include.junctions:
            add_elements('intron')
        if ref_elements_to_include.promoters:
            add_elements('promoter')
        if ref_elements_to_include.polya_sites:
            add_elements('polya')
        if ref_elements_to_include.TSS:
            add_elements('tss_exon')
        if ref_elements_to_include.TES:
            add_elements('tes_exon')
        if ref_elements_to_include.TES:
            add_elements('exon')
    
    for contig_strand, elements in ref_elements.iteritems():
        for element_type, val in elements.iteritems():
            ref_elements[contig_strand][element_type] = sorted( val )
    
    return ref_elements

def find_transcribed_regions_and_jns_in_segment(
        (contig, r_start, r_stop), 
        rnaseq_reads, promoter_reads, polya_reads,
        ref_elements, ref_elements_to_include):
    #reg = numpy.array([1,7,6,0,0,0,4,0])
    #print reg, find_empty_regions(reg, min_length=4)
    #print reg, find_transcribed_regions(reg, min_empty_region_length=4)
    reg_len = r_stop-r_start+1
    cov = { '+': numpy.zeros(reg_len, dtype=float), 
            '-': numpy.zeros(reg_len, dtype=float) }
    jn_reads = {'+': defaultdict(int), '-': defaultdict(int)}
    num_unique_reads = [0.0, 0.0, 0.0]
    fragment_lengths =  defaultdict(int)
    read_mates = {}
    for reads_i,reads in enumerate((promoter_reads, rnaseq_reads, polya_reads)):
        if reads == None: continue

        ( p1_rds, p2_rds, r_plus_jns, r_minus_jns 
          ) = extract_jns_and_reads_in_region(
              (contig, '.', r_start, r_stop), reads)
        for jn, cnt in r_plus_jns.iteritems(): 
            jn_reads['+'][jn] += cnt 
        for jn, cnt in r_minus_jns.iteritems(): 
            jn_reads['-'][jn] += cnt 
        
        for qname, all_rd_data in chain(p1_rds.iteritems(), p2_rds.iteritems()):
            for read_data in all_rd_data:
                num_unique_reads[reads_i] += (
                    read_data.map_prb/2. 
                    if qname in p1_rds and qname in p2_rds 
                    else read_data.map_prb)
                for start, stop in read_data.cov_regions:
                    # if start - r_start > reg_len, then it doesn't do 
                    # anything, so the next line is not necessary
                    # if start - r_start > reg_len: continue
                    cov[read_data.strand][
                        max(0, start-r_start):max(0, stop-r_start+1)] += 1
        # update the fragment length dist
        if reads == rnaseq_reads:
            for qname, read1_data in p1_rds.iteritems():
                if (len(read1_data) != 1 
                    or len(read1_data[0].cov_regions) > 1
                    or read1_data[0].map_prb < 0.95): 
                    continue
                try: read2_data = p2_rds[qname]
                except KeyError: continue
                if (len(read2_data) != 1 
                    or len(read2_data[0].cov_regions) > 1
                    or read2_data[0].map_prb < 0.95): 
                    continue
                frag_len = calc_frag_len_from_read_data(
                    read1_data[0], read2_data[0])
                fragment_lengths[frag_len] += read1_data[0].map_prb
    
    # add pseudo coverage for annotated jns. This is so that the clustering
    # algorithm knows which gene segments to join if a jn falls outside of 
    # a region with observed transcription
    # we also add pseudo coverage for other elements to provide connectivity
    if ref_elements != None and len(ref_elements_to_include) > 0:
        ref_jns = []
        for strand in "+-":
            for element_type, (start, stop) in ref_elements.iter_elements(
                    contig, strand, 
                    r_start-config.MIN_INTRON_SIZE-1, 
                    r_stop+config.MIN_INTRON_SIZE+1):
                if element_type == 'intron':
                    ref_jns.append((strand, start-r_start, stop-r_start))
                    jn_reads[strand][(start,stop)] += 0
                # add in exons
                elif element_type in ref_elements_to_include:
                    cov[strand][
                        max(0,start-r_start):max(0, stop-r_start+1)] += 1
        # add in the junction coverage
        for strand, start, stop in ref_jns:
            cov[strand][max(0, start-1-config.MIN_INTRON_SIZE):
                        max(0, start-1+1)] += 1
            cov[strand][stop:stop+config.MIN_INTRON_SIZE+1] += 1
    
    transcribed_regions = {'+': [], '-': []}
    for strand, counts in cov.iteritems():
        transcribed_regions[strand].extend(
            sorted(find_transcribed_regions(counts)))
    
    jn_reads['+'] = sorted(jn_reads['+'].iteritems())
    jn_reads['-'] = sorted(jn_reads['-'].iteritems())
    
    return ( 
        transcribed_regions, jn_reads, 
        ReadCounts(*num_unique_reads), fragment_lengths )

def filter_jns(jns, antistrand_jns, whitelist=set()):
    filtered_junctions = defaultdict(int)
    jn_starts = defaultdict( int )
    jn_stops = defaultdict( int )
    for (start, stop), cnt in jns.iteritems():
        jn_starts[start] = max( jn_starts[start], cnt )
        jn_stops[stop] = max( jn_stops[stop], cnt )

    for (start, stop), cnt in jns.iteritems():
        if (start, stop) not in whitelist:
            val = beta.ppf(0.01, cnt+1, jn_starts[start]+1)
            if val < config.NOISE_JN_FILTER_FRAC: continue
            val = beta.ppf(0.01, cnt+1, jn_stops[stop]+1)
            if val < config.NOISE_JN_FILTER_FRAC: continue
            #val = beta.ppf(0.01, cnt+1, jn_grps[jn_grp_map[(start, stop)]]+1)
            #if val < config.NOISE_JN_FILTER_FRAC: continue
            try: 
                if ( (cnt+1.)/(antistrand_jns[(start, stop)]+1) <= 1.):
                    continue
            except KeyError: 
                pass
            if stop - start + 1 > config.MAX_INTRON_SIZE: continue
        filtered_junctions[(start, stop)] = cnt
    
    return filtered_junctions

def split_genome_into_segments(contig_lens, region_to_use, 
                               min_segment_length=5000):
    """Return non-overlapping segments that cover the genome.

    The segments are closed-closed, and strand specific.
    """
    if region_to_use != None:
        r_chrm, (r_start, r_stop) = region_to_use
    total_length = sum(contig_lens.values())
    segment_length = max(min_segment_length, 
                         int(total_length/float(config.NTHREADS*1000)))
    segments = []
    # sort by shorter contigs, so that the short contigs (e.g. mitochondrial)
    # whcih usually take longer to finish are started first
    for contig, contig_length in sorted(
            contig_lens.iteritems(), key=lambda x:x[1]):
        if region_to_use != None and r_chrm != contig: 
            continue
        for start in xrange(0, contig_length, segment_length):
            if region_to_use != None and r_stop < start: 
                continue
            if region_to_use != None and r_start > start+segment_length: 
                continue
            segments.append(
                (contig, start, 
                 min(contig_length, start+segment_length-1)))
    return segments

def find_segments_and_jns_worker(
        segments, 
        transcribed_regions, jns, frag_lens, lock,
        rnaseq_reads, promoter_reads, polya_reads,
        ref_elements, ref_elements_to_include,
        num_unique_reads):
    length_of_segments = segments.qsize()
    while True:
        segment = segments.get()
        if segment == 'FINISHED': 
            config.log_statement("")
            return
        config.log_statement("Finding genes and jns in %s" % str(segment) )
        ( r_transcribed_regions, r_jns, r_n_unique_reads, r_frag_lens,
            ) = find_transcribed_regions_and_jns_in_segment(
                segment, rnaseq_reads, promoter_reads, polya_reads, 
                ref_elements, ref_elements_to_include) 
        with lock:
            for length, count in r_frag_lens.iteritems():
                if length not in frag_lens:
                    frag_lens[length] = 0
                frag_lens[length] += count
            transcribed_regions[(segment[0], '+')].extend([
                (start+segment[1], stop+segment[1])
                for start, stop in r_transcribed_regions['+']])
            transcribed_regions[(segment[0], '-')].extend([
                (start+segment[1], stop+segment[1])
                for start, stop in r_transcribed_regions['-']])

            jns[(segment[0], '+')].extend(r_jns['+'])
            jns[(segment[0], '-')].extend(r_jns['-'])
            
            for i, val in enumerate(r_n_unique_reads):
                num_unique_reads[i].value += val
    
    return

def load_gene_bndry_bins( genes, contig, strand, contig_len ):  
    if config.VERBOSE:
        config.log_statement( 
            "Loading gene boundaries from annotated genes in %s:%s" % (  
                contig, strand) )  
  
    ## find the gene regions in this contig. Note that these  
    ## may be overlapping  
    gene_intervals = []  
    for gene in genes:  
        if gene.chrm != contig: continue  
        if gene.strand != strand: continue  
        gene_intervals.append((gene.start, gene.stop))  
    if len(gene_intervals) == 0: return []  
      
    ## merge overlapping genes regions by building a graph with nodes  
    ## of all gene regions, and edges with all overlapping genes   
  
    # first, find the edges by probing into the sorted intervals  
    gene_intervals.sort()  
    gene_starts = numpy.array([interval[0] for interval in gene_intervals])  
    overlapping_genes = []  
    for gene_index, (start, stop) in enumerate(gene_intervals):  
        start_i = numpy.searchsorted(gene_starts, start)  
        # start looping over potentially overlapping intervals  
        for i, gene_interval in enumerate(gene_intervals[start_i:]):  
            # if we have surpassed all potentially overlapping intervals,  
            # then we don't need to go any further  
            if gene_interval[0] > stop: break  
            # if the intervals overlap ( I dont think I need this test, but  
            # it's cheap and this could be an insidious bug )  
            if not (stop < gene_interval[0] or start > gene_interval[1] ):  
                overlapping_genes.append( (int(gene_index), int(i+start_i)) )  
      
    # buld the graph, find the connected components, and build   
    # the set of merged intervals  
    genes_graph = nx.Graph()  
    genes_graph.add_nodes_from(xrange(len(gene_starts)))  
    genes_graph.add_edges_from(overlapping_genes)  
    merged_gene_intervals = []  
    for genes in nx.connected_components(genes_graph):  
        start = min( gene_intervals[i][0] for i in genes )  
        stop = max( gene_intervals[i][1] for i in genes )  
        merged_gene_intervals.append( [start, stop] )  
      
    # expand the gene boundaries to their maximum amount such that the genes   
    # aren't overlapping. This is to allow for gene ends that lie outside of   
    # the previously annotated boundaries  
    merged_gene_intervals.sort()  
    for i in xrange(0,len(merged_gene_intervals)-1):  
        mid = (merged_gene_intervals[i][1]+merged_gene_intervals[i+1][0])/2  
        merged_gene_intervals[i][1] = int(mid)-1  
        merged_gene_intervals[i+1][0] = int(mid)+1      
    merged_gene_intervals[0][0] = max(   
        1, merged_gene_intervals[0][0]-config.MAX_GENE_EXPANSION)  
    merged_gene_intervals[-1][1] = min(   
        contig_len-1, merged_gene_intervals[-1][1]+config.MAX_GENE_EXPANSION)  
      
    # build gene objects with the intervals  
    gene_bndry_bins = []  
    for start, stop in merged_gene_intervals:  
        gene_bin = GeneElements( contig, strand )
        gene_bin.regions.append(  
            SegmentBin(start, stop, ["ESTART",], ["ESTOP",], "GENE"))
        gene_bndry_bins.append( gene_bin )  
      
    config.log_statement( "" )  
      
    return gene_bndry_bins  


def find_all_gene_segments( contig_lens,
                            rnaseq_reads, promoter_reads, polya_reads,
                            ref_genes, ref_elements_to_include,
                            region_to_use=None ):    
    # if we are supposed to use the annotation genes
    if ref_elements_to_include.genes == True:
        gene_bndry_bins = []
        for contig, contig_len in contig_lens.iteritems():
            for strand in '+-':
                contig_gene_bndry_bins = load_gene_bndry_bins(
                    ref_genes, contig, strand, contig_len)
                gene_bndry_bins.extend( contig_gene_bndry_bins )
        return gene_bndry_bins
    
    config.log_statement("Spawning gene segment finding children")    
    segments_queue = multiprocessing.Queue()
    manager = multiprocessing.Manager()
    num_unique_reads = ReadCounts(multiprocessing.Value('d', 0.0), 
                                  multiprocessing.Value('d', 0.0), 
                                  multiprocessing.Value('d', 0.0))
    transcribed_regions = {}
    jns = {}
    for strand in "+-":
        for contig in contig_lens.keys():
            transcribed_regions[(contig, strand)] = manager.list()
            jns[(contig, strand)] = manager.list()
    frag_lens = manager.dict()
    lock = multiprocessing.Lock()

    ref_element_types_to_include = set()
    if ref_elements_to_include.junctions: 
        ref_element_types_to_include.add('intron')
    if ref_elements_to_include.TSS: 
        ref_element_types_to_include.add('tss_exon')
    if ref_elements_to_include.TES: 
        ref_element_types_to_include.add('tes_exon')
    if ref_elements_to_include.promoters: 
        ref_element_types_to_include.add('promoter')
    if ref_elements_to_include.polya_sites: 
        ref_element_types_to_include.add('polya')
    if ref_elements_to_include.exons: 
        ref_element_types_to_include.add('exon')
    
    pids = []
    for i in xrange(config.NTHREADS):
        pid = os.fork()
        if pid == 0:
            find_segments_and_jns_worker(
                segments_queue, 
                transcribed_regions, jns, frag_lens, lock,
                rnaseq_reads, promoter_reads, polya_reads,
                ref_genes, ref_element_types_to_include,
                num_unique_reads)
            os._exit(0)
        pids.append(pid)

    config.log_statement("Populating gene segment queue")        
    segments = split_genome_into_segments(contig_lens, region_to_use)
    for segment in segments: 
        segments_queue.put(segment)
    for i in xrange(config.NTHREADS): segments_queue.put('FINISHED')
    
    while segments_queue.qsize() > 2*config.NTHREADS:
        config.log_statement(
            "Waiting on gene segment finding children (%i/%i segments remain)" 
            %(segments_queue.qsize(), len(segments)))        
        time.sleep(0.5)
    
    for i, pid in enumerate(pids):
        config.log_statement(
            "Waiting on gene segment finding children (%i/%i children remain)" 
            %(len(pids)-i, len(pids)))
        os.waitpid(pid, 0) 
    
    global read_counts
    read_counts = ReadCounts(*(x.value for x in num_unique_reads))
    config.log_statement(str(read_counts), log=True)

    config.log_statement("Merging gene segments")
    merged_transcribed_regions = {}
    for key, intervals in transcribed_regions.iteritems():
        merged_transcribed_regions[
            key] = merge_adjacent_intervals(
                intervals, config.MAX_EMPTY_REGION_SIZE)
    transcribed_regions = merged_transcribed_regions
    
    config.log_statement("Filtering junctions")    
    filtered_jns = defaultdict(dict)
    for contig in contig_lens.keys():
        plus_jns = defaultdict(int)
        for jn, cnt in jns[(contig, '+')]: plus_jns[jn] += cnt
        minus_jns = defaultdict(int)
        for jn, cnt in jns[(contig, '-')]: minus_jns[jn] += cnt
        filtered_jns[(contig, '+')] = filter_jns(plus_jns, minus_jns)
        filtered_jns[(contig, '-')] = filter_jns(minus_jns, plus_jns)
    
    # build the fragment length distribution
    frag_lens = dict(frag_lens)
    min_fl = max(config.MIN_FRAGMENT_LENGTH, int(min(frag_lens.keys())))
    max_fl = min(config.MAX_FRAGMENT_LENGTH, int(max(frag_lens.keys())))
    fl_density = numpy.zeros(max_fl - min_fl + 1)
    for fl, cnt in frag_lens.iteritems():
        if fl < min_fl or fl > max_fl: continue
        fl_density[fl-min_fl] += cnt
    fl_density = fl_density/fl_density.sum()
    global fl_dists
    fl_dists = {'mean': FlDist(min_fl, max_fl, fl_density)}

    # we are down with the manager
    manager.shutdown()
    
    if ref_elements_to_include.junctions:
        for gene in ref_genes:
            for jn in gene.extract_elements()['intron']:
                if jn not in filtered_jns[(gene.chrm, gene.strand)]:
                    filtered_jns[(gene.chrm, gene.strand)][jn] = 0
    
    config.log_statement("Clustering gene segments")    
    # build bins for all of the genes and junctions, converting them to 1-based
    # in the process
    new_genes = []
    new_introns = []
    for contig, contig_len in contig_lens.iteritems():
        for strand in '+-':
            key = (contig, strand)
            jns = [ (start, stop, cnt) 
                    for (start, stop), cnt 
                    in sorted(filtered_jns[key].iteritems()) ]
            for start, stop, cnt in jns:
                new_introns.append(
                    SegmentBin(start, stop, ["D_JN",], ["R_JN",], "INTRON"))
            intervals = cluster_intron_connected_segments(
                transcribed_regions[key], 
                [(start, stop) for start, stop, cnt in jns ] )
            # add the intergenic space, since there could be interior genes
            for segments in intervals: 
                new_gene = GeneElements( contig, strand )
                for start, stop in segments:
                    new_gene.regions.append( 
                        SegmentBin(start, stop, ["ESTART",], ["ESTOP",], "GENE") )
                if new_gene.stop-new_gene.start+1 < config.MIN_GENE_LENGTH: 
                    continue
                new_genes.append(new_gene)
    
    return new_genes

def find_exons( contig_lens, gene_bndry_bins, ofp,
                rnaseq_reads, cage_reads, polya_reads,
                ref_genes, ref_elements_to_include,
                junctions=None, nthreads=None):
    assert not any(ref_elements_to_include) or ref_genes != None
    if nthreads == None: nthreads = config.NTHREADS
    assert junctions == None
    
    ref_elements = extract_reference_elements( 
        ref_genes, ref_elements_to_include )
    genes_queue_lock = multiprocessing.Lock()
    threads_are_running = multiprocessing.Value('i', 0)
    if config.NTHREADS > 1:
        manager = multiprocessing.Manager()
        genes_queue = manager.list()
    else:
        genes_queue = []
     
    genes_queue.extend( gene_bndry_bins )
    args = [ (genes_queue, genes_queue_lock, threads_are_running), 
             ofp, contig_lens, ref_elements, ref_elements_to_include,
             rnaseq_reads, cage_reads, polya_reads  ]
    
    n_genes = len(genes_queue)
    if nthreads == 1:
        find_exons_worker(*args)
    else:
        config.log_statement("Waiting on exon finding children (%i/%i remain)"%(
                len(genes_queue), n_genes))
        ps = []
        for i in xrange( nthreads ):
            p = multiprocessing.Process(target=find_exons_worker, args=args)
            p.start()
            ps.append( p )
        
        while True:
            config.log_statement(
                "Waiting on exon finding children (%i/%i remain)"%(
                    len(genes_queue), n_genes))
            if all( not p.is_alive() for p in ps ):
                break
            time.sleep( 1.0 )

    config.log_statement( "" )    
    return

def find_elements( promoter_reads, rnaseq_reads, polya_reads,
                   ofname, ref_genes, ref_elements_to_include, 
                   region_to_use=None):
    # wrap everything in a try block so that we can with elegantly handle
    # uncaught exceptions
    try:
        ofp = ThreadSafeFile( ofname + "unfinished", "w" )
        ofp.write(
            'track name="%s" visibility=2 itemRgb="On" useScore=1\n' % ofname)
        
        contigs, contig_lens = get_contigs_and_lens( 
            [ reads for reads in [rnaseq_reads, promoter_reads, polya_reads]
              if reads != None ] )
        contig_lens = dict(zip(contigs, contig_lens))
        regions = []
        for contig, contig_len in contig_lens.iteritems():
            if region_to_use != None and contig != region_to_use: continue
            for strand in '+-':
                regions.append( (contig, strand, 0, contig_len) )        
        
        # load the reference elements
        config.log_statement("Finding gene segments")
        gene_segments = find_all_gene_segments( 
            contig_lens, 
            rnaseq_reads, promoter_reads, polya_reads,
            ref_genes, ref_elements_to_include, 
            region_to_use=region_to_use )
        # sort genes from longest to shortest. This should help improve the 
        # multicore performance
        gene_segments.sort( key=lambda x: x.stop-x.start, reverse=True )
        find_exons( contig_lens, gene_segments, ofp,
                    rnaseq_reads, promoter_reads, polya_reads,
                    ref_genes, ref_elements_to_include, 
                    junctions=None, nthreads=config.NTHREADS )            
    except Exception, inst:
        config.log_statement( "FATAL ERROR", log=True )
        config.log_statement( traceback.format_exc(), log=True, display=False )
        ofp.close()
        raise
    else:
        ofp.close()
    
    shutil.move(ofname + "unfinished", ofname)
    return ofname