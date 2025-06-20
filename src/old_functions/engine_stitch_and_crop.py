# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
"""
Train and eval functions used in main.py
"""
import math
import sys
from typing import Iterable
import numpy as np
import PIL
import torch
import cv2
from pathlib import Path
from tqdm import tqdm
from ast import literal_eval
import ffmpeg
import time
from skimage.measure import label

from .util import data_viz
from .util import misc as utils
from .util import box_ops
from .util import data_viz
from .datasets.transforms import Normalize,ToTensor,Compose

def calc_loss_for_training_methods(outputs,
                                   targets,
                                   criterion,
                                   ):

    outputs_split = {}
    losses = {}
    training_methods =  outputs['training_methods'] # dn_object, dn_track, dn_enc
    outputs_split = {}

    for training_method in training_methods:

        target_TM = targets[0][training_method]
        outputs_TM = utils.split_outputs(outputs,target_TM)
        
        if training_method == 'main':
            if 'two_stage' in outputs:
                outputs_TM['two_stage'] = outputs['two_stage']
                outputs_split['two_stage'] = outputs['two_stage']
            if 'OD' in outputs:
                outputs_TM['OD'] = outputs['OD']
                outputs_split['OD'] = outputs['OD']

        outputs_split[training_method] = outputs_TM
        
        losses = criterion(outputs_TM, targets, losses, training_method)
        
    return outputs_split, losses

def train_one_epoch(model: torch.nn.Module, 
                    criterion: torch.nn.Module,
                    data_loader: Iterable, 
                    optimizer: torch.optim.Optimizer,
                    epoch: int, 
                    args,  
                    interval: int = 50):

    dataset = 'train'
    model.train()
    criterion.train()

    ids = np.concatenate(([0],np.random.randint(0,len(data_loader),args.num_plots)))

    metrics_dict = {}

    for i, (samples, targets) in enumerate(data_loader):

        samples = samples.to(args.device)
        targets = [utils.nested_dict_to_device(t, args.device) for t in targets]

        outputs, targets, _, _, _ = model(samples,targets)

        del _
        torch.cuda.empty_cache()

        outputs, loss_dict = calc_loss_for_training_methods(outputs, targets, criterion)
        
        weight_dict = criterion.weight_dict

        losses = sum(loss_dict[k] * weight_dict[k] for k in loss_dict.keys())
        loss_dict['loss'] = losses

        if not math.isfinite(losses.item()):
            print(f"Loss is {losses.item()}, stopping training")
            sys.exit(1)

        optimizer.zero_grad()
        losses.backward()

        if args.clip_max_norm > 0:
            torch.nn.utils.clip_grad_norm_(model.parameters(), args.clip_max_norm)

        optimizer.step()

        if i == 0:
            lr = np.zeros((1,len(optimizer.param_groups)))
            for p,param_group in enumerate(optimizer.param_groups):
                lr[0,p] = param_group['lr']

        main_targets = [target['main']['cur_target'] for target in targets]

        acc_dict = {}
        if args.tracking:
            acc_dict = utils.calc_track_acc(acc_dict,outputs['main'],main_targets,args)
        else:
            acc_dict = utils.calc_bbox_acc(acc_dict,outputs['main'],main_targets,args)

        if args.num_OD_layers > 0:
            OD_targets = [target['OD']['cur_target'] for target in targets]
            acc_dict = utils.calc_bbox_acc(acc_dict,outputs['OD'],OD_targets,args)

        metrics_dict = utils.update_metrics_dict(metrics_dict,acc_dict,loss_dict,weight_dict,i,lr)

        if (i in ids and (epoch % 5 == 0 or epoch == 1)) and args.data_viz:
            data_viz.plot_results(outputs, targets,samples.tensors, args.output_dir, folder=dataset + '_outputs', filename = f'Epoch{epoch:03d}_Step{i:06d}.png', args=args)

        if i > 0 and (i % interval == 0 or i == len(data_loader) - 1):
            utils.display_loss(metrics_dict,i,len(data_loader),epoch=epoch,dataset=dataset)
    
    return metrics_dict

@torch.no_grad()
def evaluate(model, criterion, data_loader, args, epoch: int = None, interval=50):

    model.eval()
    criterion.eval()
    dataset = 'val'
    ids = np.concatenate(([0],np.random.randint(0,len(data_loader),args.num_plots)))

    metrics_dict = {}
    for i, (samples, targets) in enumerate(data_loader):
         
        samples = samples.to(args.device)
        targets = [utils.nested_dict_to_device(t, args.device) for t in targets]

        outputs, targets, _, _, _ = model(samples,targets)
        outputs, loss_dict = calc_loss_for_training_methods(outputs, targets, criterion)

        weight_dict = criterion.weight_dict

        losses = sum(loss_dict[k] * weight_dict[k] for k in loss_dict.keys())
        loss_dict['loss'] = losses

        main_targets = [target['main']['cur_target'] for target in targets]

        acc_dict = {}
        if args.tracking:
            acc_dict = utils.calc_track_acc(acc_dict,outputs['main'],main_targets,args)
        else:
            acc_dict = utils.calc_bbox_acc(acc_dict,outputs['main'],main_targets,args)

        if 'OD' in outputs:
            OD_targets = [target['OD']['cur_target'] for target in targets]
            acc_dict = utils.calc_bbox_acc(acc_dict,outputs['OD'],OD_targets,args)

        metrics_dict = utils.update_metrics_dict(metrics_dict,acc_dict,loss_dict,weight_dict,i)

        if i in ids and (epoch % 5 == 0 or epoch == 1) and args.data_viz:
            data_viz.plot_results(outputs, targets,samples.tensors, args.output_dir, folder=dataset + '_outputs', filename = f'Epoch{epoch:03d}_Step{i:06d}.png', args=args)

        if i > 0 and (i % interval == 0  or i == len(data_loader) - 1):
            utils.display_loss(metrics_dict,i,len(data_loader),epoch=epoch,dataset=dataset)

    return metrics_dict


@torch.no_grad()
class pipeline():
    def __init__(self,model, fps, args, display_all_aux_outputs=False):
        
        self.model = model
        self.crop_and_stitch = True
        self.display_all_aux_outputs = display_all_aux_outputs

        # Make a new folder with CTC folder number
        self.output_dir = args.output_dir / fps[0].parts[-2]
        self.output_dir.mkdir(exist_ok=True)

        if self.display_all_aux_outputs:
            args.hooks = False

        if args.hooks:
            self.use_hooks = True
            (self.output_dir / 'attn_weight_maps').mkdir(exist_ok=True)
        else:
            self.use_hooks = False

        self.normalize = Compose([ToTensor(), Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])])

        self.threshold = 0.5
        self.mask_threshold = 0.5
        self.target_size = args.target_size
        self.alpha = 0.4

        # Because target size is stored as a string within the yaml file, we have to convert it back into a number
        if isinstance(self.target_size[0],str):
            self.target_size = literal_eval(self.target_size)

        # Convert args into class attributes
        self.args = args
        self.num_queries = args.num_queries
        self.device = args.device
        self.masks = args.masks
        self.use_dab = args.use_dab
        self.two_stage = args.two_stage
        self.return_intermediate_masks = args.return_intermediate_masks

        self.write_video = True
        self.track = args.tracking

        np.random.seed(1)

        # Set colors; we set the first six colors as primary colors
        self.all_colors = np.array([tuple((255*np.random.random(3))) for _ in range(10000)])
        self.all_colors[:6] = np.array([[0.,0.,255.],[0.,255.,0.],[255.,0.,0.],[255.,0.,255.],[0.,255.,255.],[255.,255.,0.]])
        self.all_colors = np.concatenate((self.all_colors, np.array([[127.5,127.5,0.],[0.,127.5,0.],[0.,0.,127.5],[0.,127.5,127.5],[127.5,0.,0.],[255.,127.5,255.],[255.,127.5,0.],[127.5,255.,127.5],[255.,255.,127.5],[127.5,127.5,255.],[50.,200,200],[255.,127.5,127.5],[75,75,150.],[127.5,255.,255.],[255.,255.,255.],[127.5,127.5,127.5]])))

        if args.two_stage:
            (self.output_dir / 'two_stage').mkdir(exist_ok=True)
            self.display_object_query_boxes = False # If two-stage is used, OQ's are not using learned positional embeddings so this is useful to display
        else:
            self.display_object_query_boxes = True 
            self.query_box_locations = [np.zeros((1,4)) for i in range(args.num_queries)]

            final_fmap = np.array([self.target_size[0] // 32, self.target_size[1] // 32])
            self.enc_map = [np.zeros((final_fmap[0]*2**f,final_fmap[1]*2**f)) for f in range(args.num_feature_levels)][::-1]

        if self.display_all_aux_outputs:
            self.display_decoder_aux = True
        else:            
            self.display_decoder_aux = False

        if self.display_decoder_aux:
            (self.output_dir / self.predictions_folder / 'decoder_bbox_outputs').mkdir(exist_ok=True)
            self.num_decoder_frames = 1

        self.fps = fps
        img = PIL.Image.open(fps[0],mode='r')
        
        if self.args.dataset == '2D':
            self.resize = False
            
            if self.crop_and_stitch:
                images, loc_y, loc_x = self.create_windows(img,self.target_size)
                self.loc_y, self.loc_x = loc_y, loc_x
            else:
                x,y = img.size[0] // 2, img.size[1] // 2
                self.loc_y = [(y - self.target_size[0]//2,y + self.target_size[0] //2)]
                self.loc_x = [(x - self.target_size[1]//2,x + self.target_size[1]//2)]
                loc_y = self.loc_y
                loc_x = self.loc_x 
        else:
            self.resize = True
            self.loc_y, self.loc_x = [(0,self.target_size[0])],[(0,self.target_size[1])] 
            loc_y = self.loc_y
            loc_x = self.loc_x 

        if self.resize:
            self.color_stack = np.zeros((len(fps),len(self.loc_y),len(self.loc_x),img.size[1],img.size[0]),3)
        else:
            self.color_stack = np.zeros((len(fps),len(self.loc_y),len(self.loc_x),self.target_size[0],self.target_size[1]),3)
            self.label_masks = np.zeros((len(fps),len(self.loc_y)*len(self.loc_x),self.target_size[0],self.target_size[1]))
        
        self.res_tracks = np.empty((len(loc_y),len(loc_x)),dtype=object)
        self.track_masks = np.zeros((len(fps),img.size[1],img.size[0]))
            
    def update_query_box_locations(self,pred_boxes,keep,keep_div):
        # This is only used to display to reference points for object queries that are detected
        # Get x,y location of all detected object queries 
        all_oq_boxes = pred_boxes[-self.num_queries:].cpu().numpy()
        oq_boxes = all_oq_boxes[keep[-self.num_queries:],:4]

        oq_boxes[:,1::2] = np.clip(oq_boxes[:,1::2] * self.target_size[0], 0, self.target_size[0])
        oq_boxes[:,0::2] = np.clip(oq_boxes[:,::2] * self.target_size[1], 0, self.target_size[1])

        oq_indices = keep[-self.num_queries:].nonzero()[0]
        for oq_ind, oq_box in zip(oq_indices,oq_boxes):
            self.query_box_locations[oq_ind] = np.append(self.query_box_locations[oq_ind], oq_box[None],axis=0)

    def split_up_divided_cells(self):

        self.div_track = -1 * np.ones((len(self.track_indices) + len(self.div_indices)),dtype=np.uint16) # keeps track of which cells were the result of cell division

        for div_ind in self.div_indices:
            ind = np.where(self.track_indices==div_ind)[0][0]

            self.max_cellnb += 1             
            self.cells = np.concatenate((self.cells[:ind+1],[self.max_cellnb],self.cells[ind+1:])) # add daughter cellnb after mother cellnb
            self.track_indices = np.concatenate((self.track_indices[:ind],self.track_indices[ind:ind+1],self.track_indices[ind:])) # order doesn't matter here since they are the same track indices

            self.div_track[ind:ind+2] = div_ind 

        self.new_cells = self.cells == 0

        if 0 in self.cells:
            self.max_cellnb += 1   
            self.cells[self.cells==0] = np.arange(self.max_cellnb,self.max_cellnb+sum(self.cells==0),dtype=np.uint16)
            
            assert np.max(self.cells) >= self.max_cellnb
            self.max_cellnb = np.max(self.cells)

    def update_div_boxes(self,boxes,masks=None):
        # boxes where div_indices were repeat; now they need to be rearrange because only the first box is sent to decoder
        unique_divs = np.unique(self.div_track[self.div_track != -1])
        for unique_div in unique_divs:
            div_ids = (self.div_track == unique_div).nonzero()[0]
            boxes[div_ids[1],:4] = boxes[div_ids[0],4:]
            # e.g. [[15, 45, 16, 22, 14, 62, 15, 20]   -->  [[15, 45, 16, 22, 14, 62, 15, 20]   -->  [[15, 45, 16, 22]  --> fed to decoder
            #       [15, 45, 16, 22, 14, 62, 15, 20]]  -->   [14, 62, 15, 20, 14, 62, 15, 20]]  -->   [14, 62, 15, 20]]  --> fed to decoder

            if masks is not None:
                masks[div_ids[1],:1] = masks[div_ids[0],1:] 

        return boxes[:,:4], masks[:,0]

    def create_windows(self, img, target_size, min_overlap = 64):
        """
        Crop input image into windows of set size.

        Parameters
        ----------
        img : 2D array
            Input image.
        target_size : tuple, optional
            Dimensions of the windows to crop out.
            The default is (512,512).
        min_overlap : int, optional
            Minimum overlap between windows in pixels.
            Defaul is 24.


        Returns
        -------

        windows: 3D array
            Cropped out images to feed into U-Net. Dimensions are
            (nb_of_windows, target_size[0], target_size[1])
        loc_y : list
            List of lower and upper bounds for windows over the y axis
        loc_x : list
            List of lower and upper bounds for windows over the x axis

        """
        # Make sure image is minimum shape (bigger than the target_size)

        img = np.array(img)

        if img.shape[0] < target_size[0]:
            img = np.concatenate(
                (img, np.zeros((target_size[0] - img.shape[0], img.shape[1]))), axis=0
            )
        if img.shape[1] < target_size[1]:
            img = np.concatenate(
                (img, np.zeros((img.shape[0], target_size[1] - img.shape[1]))), axis=1
            )

        # Decide how many images vertically the image is split into
        ny = int(
            1 + float(img.shape[0] - min_overlap) / float(target_size[0] - min_overlap)
        )
        nx = int(
            1 + float(img.shape[1] - min_overlap) / float(target_size[1] - min_overlap)
        )
        # If image is 512 pixels or smaller, there is no need for anyoverlap
        if img.shape[0] == target_size[0]:
            ny = 1
        if img.shape[1] == target_size[1]:
            nx = 1

        # Compute y-axis indices:
        ovlp_y = -(img.shape[0] - ny * target_size[0]) / (ny - 1) if ny > 1 else 0
        loc_y = []
        for n in range(ny - 1):
            loc_y += [
                (
                    int(target_size[0] * n - ovlp_y * n),
                    int(target_size[0] * (n + 1) - ovlp_y * n),
                )
            ]
        loc_y += [(img.shape[0] - target_size[0], img.shape[0])]

        # Compute x-axis indices:
        ovlp_x = -(img.shape[1] - nx * target_size[1]) / (nx - 1) if nx > 1 else 0
        loc_x = []
        for n in range(nx - 1):
            loc_x += [
                (
                    int(target_size[1] * n - ovlp_x * n),
                    int(target_size[1] * (n + 1) - ovlp_x * n),
                )
            ]

        loc_x += [(img.shape[1] - target_size[1], img.shape[1])]

        # Store all cropped images into one numpy array called windows
        windows = []
        for i in range(len(loc_y)):
            for j in range(len(loc_x)):
                windows.append(PIL.Image.fromarray(img[loc_y[i][0] : loc_y[i][1], loc_x[j][0] : loc_x[j][1]]))

        return windows, loc_y, loc_x


    def stitch_edge_cells(self,index, res_cellnb, shift_y, shift_x):
        other_res_arr = self.result_stacked[index,self.loc_y[self.i][0]:self.loc_y[self.i][1],self.loc_x[self.j][0]:self.loc_x[self.j][1]][self.result[self.index] == res_cellnb]
        counts = np.bincount(other_res_arr)

        y0,y1,x0,x1 = self.loc[self.index]
        res_crop = self.res_crops[self.index]
        prev_res = self.results[self.framenb-1,self.index, y0 - self.loc_y[self.i][0] : res_crop[0], x0 - self.loc_x[self.j][0] : res_crop[1]]

        if len(counts) > 1:
            other_res_cellnb = np.argmax(counts[1:]) + 1

            y0,y1,x0,x1 = self.loc[index]
            res_crop = self.res_crops[index]
            other_res = self.result[index, y0 - self.loc_y[self.i+shift_y][0] : res_crop[0], x0 - self.loc_x[self.j+shift_x][0] : res_crop[1]]

            if self.framenb > 0 and not self.div: # check if cell tracks to a previous cell label
                other_res = self.results[self.framenb,index, y0 - self.loc_y[self.i+shift_y][0] : res_crop[0], x0 - self.loc_x[self.j+shift_x][0] : res_crop[1]]
                prev_other_res = self.results[self.framenb-1,index, y0 - self.loc_y[self.i+shift_y][0] : res_crop[0], x0 - self.loc_x[self.j+shift_x][0] : res_crop[1]]
                prev_other_stitch = self.stitches[self.framenb-1,y0:y1, x0:x1] 


                if other_res_cellnb in prev_other_res and (res_cellnb not in prev_res or (prev_res == res_cellnb).sum() < (prev_other_res == other_res_cellnb).sum()): # Cell in adjacent crop tracks from a previous cell - tracking
                    array = prev_other_stitch[prev_other_res == other_res_cellnb]
                    counts = np.bincount(array)
                    if len(counts) > 1:
                        old_cell_label = self.cell_label
                        self.cell_label = np.argmax(counts[1:]) + 1

                        if old_cell_label in self.stitch:
                            self.stitch[self.stitch == old_cell_label] = self.cell_label

                        if self.stitch[y0:y1,x0:x1][other_res == other_res_cellnb].sum() == 0:
                            self.stitch[y0:y1,x0:x1][other_res == other_res_cellnb] = self.cell_label

                    array = self.stitch[self.loc_y[self.i+shift_y][0]:self.loc_y[self.i+shift_y][1],self.loc_x[self.j+shift_x][0]:self.loc_x[self.j+shift_x][1]][self.result[index] == other_res_cellnb]
                    counts = np.bincount(array)
    
                    if len(counts) > 1:
                        self.cell_label = np.argmax(counts[1:]) + 1
                        self.stitch[y0:y1,x0:x1][other_res == other_res_cellnb] = self.cell_label

            else:
                if self.stitch[y0:y1,x0:x1][other_res == other_res_cellnb].sum() == 0:
                    array = self.stitch[self.loc_y[self.i+shift_y][0]:self.loc_y[self.i+shift_y][1],self.loc_x[self.j+shift_x][0]:self.loc_x[self.j+shift_x][1]][self.result[index] == other_res_cellnb]
                    counts = np.bincount(array)
                    if len(counts) > 1:
                        self.cell_label = np.argmax(counts[1:]) + 1
                        self.stitch[y0:y1,x0:x1][other_res == other_res_cellnb] = self.cell_label

    def get_new_cellnb(self):
        return np.max(self.stitches) + 1

    def stitch_pic(self, results,fps):
        """
        Stitch segmentation back together from the windows of create_windows()

        Parameters
        ----------
        results : 3D array
            Segmentation outputs from the seg model with dimensions
            (nb_of_windows, target_size[0], target_size[1])
        ----------
        loc_y : list
            List of lower and upper bounds for windows over the y axis
        loc_x : list
            List of lower and upper bounds for windows over the x axis

        Returns
        -------
        stitch_norm : 2D array
            Stitched image.

        """
        self.results = results.astype(np.uint16)
        self.stitches = np.zeros((len(self.results),self.loc_y[-1][1], self.loc_x[-1][1]), dtype=self.results.dtype)
        man_track = None
        movie = np.zeros((len(self.results),self.loc_y[-1][1], self.loc_x[-1][1],3), dtype=self.results.dtype)
        ind_array = np.zeros((self.loc_y[-1][1], self.loc_x[-1][1]), dtype=self.results.dtype)
        frame_edge = np.zeros((self.loc_y[-1][1], self.loc_x[-1][1]),dtype=self.results.dtype)
        frame_edge[1:-1,0] = 1
        frame_edge[0,1:-1] = 2
        frame_edge[1:-1,-1] = 3
        frame_edge[-1,1:-1] = 4
        frame_edge[0,0] = 12
        frame_edge[-1,0] = 14
        frame_edge[0,-1] = 32
        frame_edge[-1,-1] = 34

        self.target_edge_grid = np.zeros((self.loc_y[0][-1],self.loc_x[0][-1]),dtype=np.uint8)
        self.target_edge_grid[1:-1,0] = 1
        self.target_edge_grid[0,1:-1] = 2
        self.target_edge_grid[1:-1,-1] = 3
        self.target_edge_grid[-1,1:-1] = 4
        self.target_edge_grid[0,0] = 12
        self.target_edge_grid[-1,0] = 14
        self.target_edge_grid[0,-1] = 32
        self.target_edge_grid[-1,-1] = 34

        self.loc = []
        self.res_crops = []

        y_end = 0
        self.index = 0
        for i in range(len(self.loc_y)):
            
            # Compute y location of window:
            y_start = y_end
            if i + 1 == len(self.loc_y):
                y_end = self.loc_y[i][1]
            else:
                y_end = int((self.loc_y[i][1] + self.loc_y[i + 1][0]) / 2)

            x_end = 0
            for j in range(len(self.loc_x)):

                # Compute x location of window:
                x_start = x_end
                if j + 1 == len(self.loc_x):
                    x_end = self.loc_x[j][1]
                else:
                    x_end = int((self.loc_x[j][1] + self.loc_x[j + 1][0]) / 2)

                self.loc.append([y_start,y_end,x_start,x_end])
                
                res_crop_y = -(self.loc_y[i][1] - y_end) if self.loc_y[i][1] - y_end > 0 else None
                res_crop_x = -(self.loc_x[j][1] - x_end) if self.loc_x[j][1] - x_end > 0 else None

                self.res_crops.append([res_crop_y,res_crop_x])

                ind_array[y_start:y_end, x_start:x_end] = self.index
                self.index += 1

        for framenb, (self.result,self.stitch, fp) in enumerate(zip(self.results,self.stitches,fps)):

            self.framenb = framenb
            self.result_stacked = np.zeros((len(self.result),self.loc_y[-1][1], self.loc_x[-1][1]), dtype=self.results.dtype)
            self.parent_lineage = {}

            # Create an array to store segmentations into a format similar to how the image was cropped
            self.index = 0
            for i in range(len(self.loc_y)):
                for j in range(len(self.loc_x)):
                    self.result_stacked[self.index,self.loc_y[i][0]:self.loc_y[i][1],self.loc_x[j][0]:self.loc_x[j][1]] = self.result[self.index]

                    self.index += 1

            # Create an array to store segmentations into a format similar to how the image was cropped
            self.index = 0
            y_end = 0
            for i in range(len(self.loc_y)):

                # Compute y location of window:
                y_start = y_end
                if i + 1 == len(self.loc_y):
                    y_end = self.loc_y[i][1]
                else:
                    y_end = int((self.loc_y[i][1] + self.loc_y[i + 1][0]) / 2)

                x_end = 0
                for j in range(len(self.loc_x)):

                    self.res_track = self.res_tracks[i,j]

                    # Compute x location of window:
                    x_start = x_end
                    if j + 1 == len(self.loc_x):
                        x_end = self.loc_x[j][1]
                    else:
                        x_end = int((self.loc_x[j][1] + self.loc_x[j + 1][0]) / 2)

                    # Add to array:
                    res_crop_y = -(self.loc_y[i][1] - y_end) if self.loc_y[i][1] - y_end > 0 else None
                    res_crop_x = -(self.loc_x[j][1] - x_end) if self.loc_x[j][1] - x_end > 0 else None

                    self.res = self.result[self.index, y_start - self.loc_y[i][0] : res_crop_y, x_start - self.loc_x[j][0] : res_crop_x]
                    res_cellnbs = np.unique(self.res)
                    res_cellnbs = res_cellnbs[res_cellnbs != 0]

                    if framenb > 0:
                        prev_res = self.results[framenb-1,self.index, y_start - self.loc_y[i][0] : res_crop_y, x_start - self.loc_x[j][0] : res_crop_x]
                        prev_res_nonrestricted = self.results[framenb-1,self.index]
                        prev_stitch = self.stitches[framenb-1,y_start:y_end, x_start:x_end] 
                        prev_stitch_nonrestricted = self.stitches[framenb-1,self.loc_y[i][0]:self.loc_y[i][1],self.loc_x[j][0]:self.loc_x[j][1]]

                    for res_cellnb in res_cellnbs:

                        if (self.res == res_cellnb).sum() < 5:
                            continue

                        self.div = False

                        # Check if cell exists in previous stitch
                        if framenb > 0 and res_cellnb in prev_res:
                            prev_stitch_array = prev_stitch[prev_res == res_cellnb]
                            counts = np.bincount(prev_stitch_array)
                            if len(counts) > 1:
                                self.cell_label = np.argmax(counts[1:]) + 1
                            else:
                                self.cell_label = self.get_new_cellnb()
                        else: # If cell does not exist in previous stitch, assing a new number; this could change if it is touching edge of crop and adjacent cell is labeled
                            self.cell_label = self.get_new_cellnb()

                        # Check if cell is already in stitch; during divisions or edge cells, cells from other crops are used to make a coherent stitch
                        if self.stitch[y_start:y_end, x_start:x_end][self.res == res_cellnb].all():

                            if self.res_track[self.res_track[:,0]==res_cellnb,1][0] == framenb and self.res_track[self.res_track[:,0]==res_cellnb,-1][0] > 0:
                                stitch_cellnb = np.argmax(np.bincount(self.stitch[y_start:y_end, x_start:x_end][self.res == res_cellnb])[1:]) + 1
                                parent_res_cellnb = self.res_track[self.res_track[:,0]==res_cellnb,-1][0]
                                other_res_cellnb = self.res_track[(self.res_track[:,-1] == parent_res_cellnb)*(self.res_track[:,0] != res_cellnb),0]

                                prev_div = False
                                if stitch_cellnb in man_track[:,0]:
                                    prev_div = man_track[man_track[:,0]==stitch_cellnb,1][0] == framenb-1 and man_track[man_track[:,0]==stitch_cellnb,-1][0] > 0

                                if stitch_cellnb in self.stitches[framenb-1] and other_res_cellnb in self.res and self.stitch[self.loc_y[i][0]:self.loc_y[i][1],self.loc_x[j][0]:self.loc_x[j][1]][self.result[self.index] == other_res_cellnb].sum() > 0 and not prev_div:

                                    self.stitch[self.loc_y[i][0]:self.loc_y[i][1],self.loc_x[j][0]:self.loc_x[j][1]][self.result[self.index] == res_cellnb] = self.cell_label 

                                    div_cell_label = self.get_new_cellnb()
                                    self.stitch[self.loc_y[i][0]:self.loc_y[i][1],self.loc_x[j][0]:self.loc_x[j][1]][self.result[self.index] == other_res_cellnb] = div_cell_label 

                                    if self.stitch[self.stitch == stitch_cellnb].sum() > 0:
                                        self.stitch[self.stitch == stitch_cellnb] = 0

                                    self.parent_lineage[div_cell_label] = stitch_cellnb 
                                    self.parent_lineage[self.cell_label] = stitch_cellnb 

                            continue
                        elif self.stitch[y_start:y_end, x_start:x_end][self.res == res_cellnb].sum() > 50:
                            stitch_cellnbs_array = self.stitch[y_start:y_end, x_start:x_end][self.res == res_cellnb]
                            counts = np.bincount(stitch_cellnbs_array)
                            stitch_cellnb = np.argmax(counts[1:]) + 1
                            self.stitch[y_start:y_end, x_start:x_end][self.res == res_cellnb] = stitch_cellnb
                            continue
                        elif self.stitch[y_start:y_end, x_start:x_end][self.res == res_cellnb].sum() > 0:
                            a = 0

                        # Assign cell with new cell label
                        self.stitch[y_start:y_end, x_start:x_end][self.res == res_cellnb] = self.cell_label

                        # Check if cell divides
                        if self.res_track[self.res_track[:,0]==res_cellnb,1][0] == framenb and self.res_track[self.res_track[:,0]==res_cellnb,-1][0] > 0:
                            # Get parent cellnb from this crop index
                            parent_res_cellnb = self.res_track[self.res_track[:,0]==res_cellnb,-1][0]
                            # Get other cellnb that divided from parent_res_cellnb 
                            other_res_cellnb = self.res_track[(self.res_track[:,-1] == parent_res_cellnb)*(self.res_track[:,0] != res_cellnb),0]

                            # If divided cells are perfectly split between crop lines, we have to pick one
                            res_cellnb_indices = ind_array[self.loc_y[i][0]:self.loc_y[i][1],self.loc_x[j][0]:self.loc_x[j][1]][self.result[self.index] == res_cellnb]
                            other_res_cellnb_indices = ind_array[self.loc_y[i][0]:self.loc_y[i][1],self.loc_x[j][0]:self.loc_x[j][1]][self.result[self.index] == other_res_cellnb]
                            indices_intersect = np.intersect1d(res_cellnb_indices, other_res_cellnb_indices)

                            # If other cell division has been predicted then the current cell should be used even if it isn't best crop; 
                            other_cellnb_area =  self.stitch[self.loc_y[i][0]:self.loc_y[i][1],self.loc_x[j][0]:self.loc_x[j][1]][self.result[self.index] == other_res_cellnb].sum()

                            array = prev_stitch_nonrestricted[prev_res_nonrestricted==parent_res_cellnb]
                            counts = np.bincount(array)

                            if len(counts) > 1:
                                prev_stitch_res_cellnb = np.argmax(counts[1:]) + 1
                                div_in_prev_frame = man_track[man_track[:,0] == prev_stitch_res_cellnb,1] == framenb-1 and man_track[man_track[:,0] == prev_stitch_res_cellnb,-1]>0
                            else:
                                div_in_prev_frame = False

                            # Ignore division if divided cell is not in the restricted crop
                            if ((other_res_cellnb in self.res and parent_res_cellnb in prev_res) or (indices_intersect.size == 0 and parent_res_cellnb in prev_res_nonrestricted) or other_cellnb_area > 0) and not div_in_prev_frame:# 

                                if (other_res_cellnb in self.res and parent_res_cellnb in prev_res):
                                    array = prev_stitch[prev_res == parent_res_cellnb]
                                else:
                                    array = prev_stitch_nonrestricted[prev_res_nonrestricted == parent_res_cellnb]
                                counts = np.bincount(array)

                                # Check parent cell is present in the previous frame (restricted crop, not just the crop)
                                if len(counts) > 1: 

                                    parent_stitch_cellnb = np.argmax(counts[1:]) + 1

                                    if parent_stitch_cellnb not in self.parent_lineage.values():

                                        # Get overlap between stitch and the divided cell; see if it already has a cell label
                                        other_res_arr = self.stitch[self.loc_y[i][0]:self.loc_y[i][1],self.loc_x[j][0]:self.loc_x[j][1]][self.result[self.index] == other_res_cellnb]
                                        counts = np.bincount(other_res_arr)

                                        # Divided cell has already been labeled by another adjacent crop
                                        if len(counts) > 1: 
                                            div_cell_label = np.argmax(counts[1:]) + 1
                                            # If one crop predicts no division but an adjacent crop does, the old crop needs to be updated
                                            if div_cell_label == parent_stitch_cellnb:
                                                div_cell_label = self.get_new_cellnb()
                                                self.stitch[self.stitch==parent_stitch_cellnb] = div_cell_label
                                        # Divided cell has not been labeled yet
                                        else: 
                                            div_cell_label = self.get_new_cellnb()

                                        # To fix any issues where divisions have in different frames across adjacent crops, we take only crop's prediction if it does not touch the edges
                                        if not ((self.result[self.index] == other_res_cellnb) * (self.target_edge_grid > 0)).any():
                                            self.stitch[self.stitch == div_cell_label] = 0

                                        if not ((self.result[self.index] == res_cellnb) * (self.target_edge_grid > 0)).any():
                                            self.stitch[self.stitch == self.cell_label] = 0

                                        self.stitch[self.loc_y[i][0]:self.loc_y[i][1],self.loc_x[j][0]:self.loc_x[j][1]][self.result[self.index] == other_res_cellnb] = div_cell_label
                                        self.stitch[self.loc_y[i][0]:self.loc_y[i][1],self.loc_x[j][0]:self.loc_x[j][1]][self.result[self.index] == res_cellnb] = self.cell_label

                                        label_div_stitch = label(self.stitch == div_cell_label)
                                        cellnbs = np.unique(label_div_stitch)
                                        cellnbs = cellnbs[cellnbs!=0]

                                        if len(cellnbs) > 1:
                                            counts = np.bincount(label_div_stitch[label_div_stitch!=0])
                                            cellnb = np.argmax(counts)
                                            self.stitch[label_div_stitch > 0][self.stitch[label_div_stitch > 0] != cellnb] = 0

                                        label_stitch = label(self.stitch == self.cell_label)
                                        cellnbs = np.unique(label_stitch)                                    
                                        cellnbs = cellnbs[cellnbs!=0]

                                        if len(cellnbs) > 1:
                                            counts = np.bincount(label_stitch[label_stitch!=0])
                                            cellnb = np.argmax(counts)
                                            self.stitch[label_stitch > 0][self.stitch[label_stitch > 0] != cellnb] = 0

                                        # self.stitch[y_start:y_end, x_start:x_end][self.res == other_res_cellnb] = div_cell_label # label divided cell only within restricted crop

                                        self.parent_lineage[div_cell_label] = parent_stitch_cellnb 
                                        self.parent_lineage[self.cell_label] = parent_stitch_cellnb 

                                        self.div = True

                        edge_grid = np.zeros(self.res.shape,dtype=np.uint8)
                        edge_grid[1:-1,0] = 1
                        edge_grid[0,1:-1] = 2
                        edge_grid[1:-1,-1] = 3
                        edge_grid[-1,1:-1] = 4
                        edge_grid[0,0] = 12
                        edge_grid[-1,0] = 14
                        edge_grid[0,-1] = 32
                        edge_grid[-1,-1] = 34

                        if ((self.res == res_cellnb) * (edge_grid > 0)).any() and ((self.result_stacked[:,self.loc_y[i][0]:self.loc_y[i][1],self.loc_x[j][0]:self.loc_x[j][1]][:,self.result[self.index] == res_cellnb] > 0).sum(0) > 1).any(): # if cell touching edge and If predictions from two different crops overlap
                            edge_nbs = edge_grid[(self.res == res_cellnb) * (edge_grid>0)]
                            edge_nbs = np.unique(edge_nbs)

                            for edge_nb in edge_nbs:
                                if edge_nb > 9:

                                    num1, num2 = int(str(edge_nb)[0]), int(str(edge_nb)[1])

                                    if num1 not in edge_nbs:
                                        edge_nbs = np.concatenate((edge_nbs,np.array([num1])))

                                    if num2 not in edge_nbs:
                                        edge_nbs = np.concatenate((edge_nbs,np.array([num2])))

                            frame_edge_nbs = frame_edge[y_start:y_end, x_start:x_end][(self.res == res_cellnb) * (edge_grid>0)]
                            frame_edge_nbs = np.unique(frame_edge_nbs)

                            self.i, self.j = i,j

                            for edge_nb in edge_nbs:
                                if (edge_nb == 1 or (edge_nb in [12,14] and 1 not in edge_nbs)) and edge_nb not in frame_edge_nbs:
                                    self.stitch_edge_cells(self.index-1, res_cellnb, shift_x=-1, shift_y=0)

                                elif (edge_nb == 2 or (edge_nb in [12,32] and 2 not in edge_nbs)) and edge_nb not in frame_edge_nbs:
                                    self.stitch_edge_cells(self.index-len(self.loc_x), res_cellnb, shift_x=0, shift_y=-1)

                                elif (edge_nb == 3 or (edge_nb in [32,34] and 3 not in edge_nbs)) and edge_nb not in frame_edge_nbs:
                                    self.stitch_edge_cells(self.index+1, res_cellnb, shift_x=1, shift_y=0)

                                elif (edge_nb == 4 or (edge_nb in [14,34] and 4 not in edge_nbs)) and edge_nb not in frame_edge_nbs:
                                    self.stitch_edge_cells(self.index+len(self.loc_x), res_cellnb, shift_x=0, shift_y=1)

                                elif edge_nb == 34 and edge_nb not in frame_edge_nbs:
                                    self.stitch_edge_cells(self.index+len(self.loc_x)+1, res_cellnb, shift_x=1, shift_y=1)

                                elif edge_nb == 14 and edge_nb not in frame_edge_nbs:
                                    self.stitch_edge_cells(self.index+len(self.loc_x)-1, res_cellnb, shift_x=-1, shift_y=1)

                                elif edge_nb == 12 and edge_nb not in frame_edge_nbs:
                                    self.stitch_edge_cells(self.index-len(self.loc_x)-1, res_cellnb, shift_x=-1, shift_y=-1)

                                elif edge_nb == 32 and edge_nb not in frame_edge_nbs:
                                    self.stitch_edge_cells(self.index-len(self.loc_x)+1, res_cellnb, shift_x=1, shift_y=-1)

                    self.index += 1

            cv2.imwrite(str(self.output_dir / self.predictions_folder / f'mask{framenb:03d}.tif'),self.stitch)

            stitch_cellnbs = np.unique(self.stitch)
            stitch_cellnbs = stitch_cellnbs[stitch_cellnbs!= 0]
            parents = []

            if man_track is None:
                for cellnb in stitch_cellnbs:
                    if man_track is not None:
                        man_track_new_cell = np.array([[cellnb,framenb,framenb,0]],dtype=int)
                        man_track = np.concatenate((man_track,man_track_new_cell))
                    else:
                        man_track = np.array([[cellnb,framenb,framenb,0]],dtype=int)
                        # assert cellnb == 1
            else:
                for cellnb in stitch_cellnbs:

                    if cellnb in man_track[:,0]:
                        man_track[man_track[:,0]==cellnb,2] = framenb
                    else:
                        if cellnb in self.parent_lineage:
                            parent = self.parent_lineage[cellnb]
                            parents.append(parent)
                        else:
                            parent = 0
                        man_track_new_cell = np.array([[cellnb,framenb,framenb,parent]])
                        man_track = np.concatenate((man_track,man_track_new_cell))

            for parent in parents:
                if (man_track[:,-1] == parent).sum() != 2:
                    man_track[man_track[:,-1] == parent,-1] = 0

            movie[framenb] = np.array(PIL.Image.open(fp,mode='r').convert('RGB'))
            
            for cellnb in stitch_cellnbs:
                mask = (self.stitch == cellnb).astype(np.uint8)
                mask_color = np.repeat(mask[:,:,None],3,axis=-1)
                mask_color[mask_color[...,0]>0] = self.all_colors[cellnb]
                movie[framenb,mask_color>0] = movie[framenb,mask_color>0]*(1-self.alpha) + mask_color[mask_color>0]*(self.alpha)

                y,x = np.where(mask[:,:]>0)
                median_y = y[np.argmin(np.abs(y - np.median(y)))]
                ind_y = np.where(y == median_y)[0]

                ind_x = np.argmin(np.abs(x[ind_y] - np.median(x[ind_y])))               
                ind = ind_y[ind_x]
                org = (x[ind]-3,y[ind])

                movie[framenb] = cv2.putText(
                    movie[framenb],
                    text = f'{cellnb}', 
                    org=org, 
                    fontFace=cv2.FONT_HERSHEY_SIMPLEX, 
                    fontScale = 0.4,
                    color = (255,255,255),
                    thickness=1,
                    )
                
            div_cellnbs = man_track[(man_track[:,1]==framenb)*(man_track[:,-1]>0),-1]
            div_cellnbs = np.unique(div_cellnbs)

            for div_cellnb in div_cellnbs:
                if len(man_track[man_track[:,-1]==div_cellnb,0]) != 2:
                    print(f'{framenb}: {div_cellnb}')
                    print(man_track[man_track[:,-1]==div_cellnb])
                    continue
                cell_1,cell_2 = man_track[man_track[:,-1]==div_cellnb,0]
                y1,x1 = np.where(self.stitch == cell_1)
                y2,x2 = np.where(self.stitch == cell_2)
                
                median_y1 = y1[np.argmin(np.abs(y1 - np.median(y1)))]
                ind_y1 = np.where(y1 == median_y1)[0]

                ind_x1 = np.argmin(np.abs(x1[ind_y1] - np.median(x1[ind_y1])))               
                ind1 = ind_y1[ind_x1]

                median_y2 = y2[np.argmin(np.abs(y2 - np.median(y2)))]
                ind_y2 = np.where(y2 == median_y2)[0]

                ind_x2 = np.argmin(np.abs(x2[ind_y2] - np.median(x2[ind_y2])))               
                ind2 = ind_y2[ind_x2]

                movie[framenb] = cv2.arrowedLine(
                    movie[framenb],
                    (int(x1[ind1]), int(y1[ind1])),
                    (int(x2[ind2]), int(y2[ind2])),
                    color=(1, 1, 1),
                    thickness=1,
                )                

            movie[framenb] = cv2.putText(
                movie[framenb],
                text = f'{framenb:03d}', 
                org=(0,10), 
                fontFace=cv2.FONT_HERSHEY_SIMPLEX, 
                fontScale = 0.4,
                color = (255,255,255),
                thickness=1,
                )

        return man_track ,movie
    
    def preprocess_img(self,fp,loc_y,loc_x):

        orig_img = PIL.Image.open(fp,mode='r')
        self.orig_img_size = orig_img.size
        if self.resize:
            img = orig_img.resize((self.target_size[1],self.target_size[0])).convert('RGB')
        else:
            region = [loc_x[0],loc_y[0],loc_x[1],loc_y[1]]
            img = orig_img.crop(region).convert('RGB')

        self.img_size = img.size

        samples = self.normalize(img)[0][None]
        samples = samples.to(self.device)

        return samples

    def forward(self):

        print(f'video {self.fps.parts[-2]}')
        
        if self.display_decoder_aux:
            random_nbs = np.random.choice(np.arange(1,len(self.fps)),self.num_decoder_frames)
            random_nbs = np.concatenate((random_nbs,random_nbs+1)) # so we can see two consecutive frames

        for idx_y in range(len(self.loc_y)):
            for idx_x in range(len(self.loc_x)):

                print(f'Processing crop {idx_y * len(self.loc_x) + idx_x + 1} / {len(self.loc_y) * len(self.loc_x)}')

                ctc_data = None

                loc_y = self.loc_y[idx_y]
                loc_x = self.loc_x[idx_x]

                for i, fp in enumerate(tqdm(self.fps)):

                    if self.use_hooks and ((self.display_decoder_aux and i in random_nbs) or (self.display_all_aux_outputs and i > 0)):
                        dec_attn_outputs = []
                        # hooks = [self.model.decoder.layers[0].self_attn.register_forward_hook(lambda self, input, output: dec_attn_outputs.append(output)),
                        #     self.model.decoder.layers[-1].self_attn.register_forward_hook(lambda self, input, output: dec_attn_outputs.append(output))]
                        hooks = [self.model.decoder.layers[layer_index].self_attn.register_forward_hook(lambda self, input, output: dec_attn_outputs.append(output)) for layer_index in range(len(self.model.decoder.layers))]

                    samples = self.preprocess_img(fp,loc_y,loc_x)

                    if not self.track or i == 0: # If object detction only, we don't feed information from the previous frame
                        targets = None
                        self.max_cellnb = 0

                    with torch.no_grad():
                        outputs, targets, _, memory, hs, _ = self.model(samples,targets=targets)

                    if targets is None:
                        targets = [{'main': {'cur_target':{}}}]

                    pred_logits = outputs['pred_logits'][0].sigmoid().detach().cpu().numpy()
                    pred_boxes = outputs['pred_boxes'][0].detach()

                    keep = (pred_logits[:,0] > self.threshold)
                    keep_div = (pred_logits[:,1] > self.threshold)
                    keep_div[-self.num_queries:] = False # disregard any divisions predicted by object queries; model should have learned not to do this anyways
                    keep_div[~keep] = False

                    if self.masks:
                        pred_masks = outputs['pred_masks'][0].sigmoid().detach()
                    else:
                        pred_masks = None

                    if i > 0:
                        prevcells = np.copy(self.cells)
                    else:
                        prevcells = None

                    self.cells = np.zeros((keep.sum()),dtype=np.uint16)
                    masks = None

                    # If no objects are detected, skip
                    if self.display_object_query_boxes and keep[-self.num_queries:].sum() > 0:
                        self.update_query_box_locations(pred_boxes,keep,keep_div)

                    if sum(keep) > 0:
                        self.track_indices = keep.nonzero()[0] # Get query indices (object or track) where a cell was detected / tracked; this is used to create track queries for next  frame
                        self.div_indices = keep_div.nonzero()[0] # Get track query indices where a cell division was tracked; object queries should not be able to detect divisions

                        if pred_logits.shape[0] > self.num_queries: # If track queries are fed to the model
                            tq_keep = keep[:len(prevcells)]
                            self.cells[:sum(tq_keep)] = prevcells[tq_keep]
                        else:
                            prevcells = None

                        self.split_up_divided_cells()

                        targets[0]['main']['cur_target']['track_query_hs_embeds'] = outputs['hs_embed'][0,self.track_indices] # For div_indices, hs_embeds will be the same; no update
                        boxes = pred_boxes[self.track_indices] # For div_indices, the boxes will be repeated and will properly updated below

                        if self.masks:
                            masks = pred_masks[self.track_indices]
                        else:
                            masks = None

                        boxes,masks = self.update_div_boxes(boxes,masks)

                        if self.masks:
                            if self.resize:
                                masks = cv2.resize(np.transpose(masks.cpu().numpy(),(1,2,0)),self.orig_img_size) # regardless of cropping / resize, segmentation is 2x smaller than the original image                                
                            else:
                                masks = cv2.resize(np.transpose(masks.cpu().numpy(),(1,2,0)),self.img_size) # regardless of cropping / resize, segmentation is 2x smaller than the original image
                            masks = masks[:,:,None] if masks.ndim == 2 else masks # cv2.resize will drop last dim if it is 1
                            masks = np.transpose(masks,(-1,0,1))
                            masks_filt = np.zeros((masks.shape))
                            argmax = np.argmax(masks,axis=0)
                            
                            for m in range(masks.shape[0]):
                                masks_filt[m,argmax==m] = masks[m,argmax==m]
                                
                            masks = masks_filt > self.mask_threshold
                            # Keep largest segmentation per mask by area
                            for m,mask in enumerate(masks):
                                if mask.sum() > 0:
                                    label_mask = label(mask)
                                    labels = np.unique(label_mask)
                                    labels = labels[labels != 0]

                                    largest_ind = np.argmax(np.array([label_mask[label_mask == label].sum() for label in labels]))
                                    new_mask = np.zeros_like(mask,dtype=bool)
                                    new_mask[label_mask == labels[largest_ind]] = True
                                    masks[m] = new_mask

                        if self.args.init_boxes_from_masks:
                            h, w = masks.shape[-2:]

                            masks_tensor = torch.tensor(masks)

                            mask_boxes = box_ops.masks_to_boxes(masks_tensor,cxcywh=True)

                            # mask_boxes = box_ops.masks_to_boxes(masks > 0.5).cuda()
                            # mask_boxes = box_ops.box_xyxy_to_cxcywh(mask_boxes) / torch.as_tensor([w, h, w, h],dtype=torch.float,device=self.device)
                            targets[0]['main']['cur_target']['track_query_boxes'] = mask_boxes.to(self.device)

                        else:
                            targets[0]['main']['cur_target']['track_query_boxes'] = boxes

                        if i == 0: # self.new_cells is used to visually highlight errors specifically for the mother machine. This is because no new cells will ever appear so I know this is an erorr
                            self.new_cells = None

                    else:
                        self.track_indices = None
                        self.div_track = None
                        if prevcells is not None:
                            self.div_track = -1 * np.ones(len(prevcells),dtype=np.uint16)
                        else:
                            self.div_track = np.ones(0,dtype=np.uint16)
                        boxes = None
                        self.new_cells = None
                        # prevcells = None
                        masks = None

                        targets = None


                    if boxes is not None: # No cells
                        assert boxes.shape[0] == len(self.cells)

                    if self.resize:
                        img = self.orig_img

                    if self.track:
                        color_frame = utils.plot_tracking_results(img,boxes,masks,self.all_colors[self.cells-1],self.div_track,self.new_cells)
                    else:
                        color_frame = utils.plot_tracking_results(img,boxes,masks,self.all_colors[:len(self.cells)],self.div_track,None)

                    # if i < 50 and fps_ROI[0].parent.name == '20':
                    #     blah = np.copy(color_frame)
                    #     if i == 0:
                    #         blah[10:12,5:20] = 255
                    #     cv2.imwrite(f'/projectnb/dunlop/ooconnor/object_detection/cell-trackformer/results/240219_moma_no_flex_div_CoMOT_track_two_stage_dn_track_dn_track_group_dab_intermediate_mask_OD_decoder_layer_use_box_as_div_ref_pts_4_enc_4_dec_layers_backprop_prev/test/CTC/pred_track/20/color_frames/frame{i:03d}.png',blah)

                    min = i * 5
                    # hr = min // 60
                    # rem_min = min % 60

                    color_frame = cv2.putText(
                        color_frame,
                        text = f'{i:03d}', 
                        # text = f'{hr:01d} hr', 
                        org=(0,10), 
                        fontFace=cv2.FONT_HERSHEY_SIMPLEX, 
                        fontScale = 0.4,
                        color = (255,255,255),
                        thickness=1,
                        )

                    # color_frame = np.concatenate((color_frame,np.zeros((color_frame.shape[0],20,3))),1)

                    # color_frame = cv2.putText(
                    #     color_frame,
                    #     # text = f'{rem_min:02d} min', 
                    #     text = f'{min:03d} min', 
                    #     org=(0,10), 
                    #     # org=(0,20), 
                    #     fontFace=cv2.FONT_HERSHEY_SIMPLEX, 
                    #     fontScale = 0.4,
                    #     color = (255,255,255),
                    #     thickness=1,
                    #     )

                    if self.resize:
                        self.color_stack[i,idx_y,idx_x,:,r*img.size[0]:(r+1)*img.size[1]] = color_frame         
                    else:
                        self.color_stack[i,idx_y,idx_x,:,r*self.target_size[1]:(r+1)*self.target_size[1]] = color_frame         

                    if self.use_hooks and ((self.display_decoder_aux and i in random_nbs) or (self.display_all_aux_outputs and i > 0)):
                        for hook in hooks:
                            hook.remove()

                        scale = 1

                        for layer_index, dec_attn_output in enumerate(dec_attn_outputs):
                            dec_attn_weight_maps = dec_attn_output[1].cpu().numpy() # [output, attention_map]
                            num_cols, num_rows = dec_attn_weight_maps.shape[-2:]
                            num_heads = 1 if dec_attn_weight_maps.ndim == 3 else dec_attn_weight_maps.shape[1]
                            dec_attn_weight_maps = np.repeat(dec_attn_weight_maps[...,None],3,axis=-1)
                            for dec_attn_weight_map in dec_attn_weight_maps: # per batch
                                for h in range(num_heads):
                                    if num_heads > 1:
                                        dec_attn_weight_map_h = dec_attn_weight_map[h]
                                    else:
                                        h = 'averaged'
                                        dec_attn_weight_map_h = dec_attn_weight_map
                                    dec_attn_weight_map_color = np.zeros((num_cols+1,num_rows+1,3))
                                    dec_attn_weight_map_color[-num_cols:,-num_rows:] = dec_attn_weight_map_h
                                    # dec_attn_weight_map_color = ((dec_attn_weight_map_color / np.max(dec_attn_weight_map_color)) * 255).astype(np.uint8)
                                    dec_attn_weight_map_color = (dec_attn_weight_map_color * 255).astype(np.uint8)

                                    for tidx in range(len(keep) - self.num_queries):
                                        dec_attn_weight_map_color[tidx+1,0] = self.all_colors[prevcells[tidx]-1]
                                        dec_attn_weight_map_color[0,tidx+1] = self.all_colors[prevcells[tidx]-1]                                

                                    # color Track queries only
                                    if False:
                                        cv2.imwrite(str(self.output_dir / self.predictions_folder / 'attn_weight_maps' / (f'self_attn_weight_map_{fp.stem}_layer_{layer_index}_head_{h}.png')),dec_attn_weight_map_color_resize)

                                    for q in range(len(keep) - self.num_queries,len(keep)):
                                        dec_attn_weight_map_color[q+1,0] = self.all_colors[-q]
                                        dec_attn_weight_map_color[0,q+1] = self.all_colors[-q]
                                    
                                    cv2.imwrite(str(self.output_dir / self.predictions_folder / 'attn_weight_maps' / (f'self_attn_map_{fp.stem}_layer_{layer_index}_head_{h}.png')),dec_attn_weight_map_color)

                                if num_heads > 1:
                                    dec_attn_weight_map_avg = (dec_attn_weight_map.mean(0) * 255).astype(np.uint8)

                                    dec_attn_weight_map_avg_color = dec_attn_weight_map_color
                                    dec_attn_weight_map_avg_color[-num_cols:,-num_rows:] = dec_attn_weight_map_avg

                                    cv2.imwrite(str(self.output_dir / self.predictions_folder / 'attn_weight_maps' / (f'self_attn_map_{fp.stem}_layer_{layer_index}_head_averaged.png')),dec_attn_weight_map_avg_color)

                    if masks is not None:

                        if sum(keep) > 0:

                            label_mask = np.zeros(masks.shape[-2:],dtype=np.uint16)

                            keep_cells = np.ones(len(self.cells),dtype=bool)
                            keep_div_cells = np.ones(len(self.div_indices),dtype=bool)
                            
                            for m, cell in enumerate(self.cells):
                                if masks[m].sum() > 0:
                                    label_mask[masks[m] > 0] = cell
                                else: # Embedding was predicted to be a cell but therer is no cell mask so we remove it
                                    keep_cells[m] = False

                                    track_ind = self.track_indices[self.cells == cell][0]
                                    keep[track_ind] = False
                            
                                    if track_ind in self.div_indices:          
                                        keep_div[track_ind] = False
                                        # self.div_indices = self.div_indices[self.div_indices != track_ind]
                                        keep_div_cells[self.div_indices == track_ind] = False

                                        other_div_cell = self.cells[(self.div_track == track_ind) * (self.cells != cell)]

                                        self.div_track[self.div_track == track_ind] = -1

                                        if prevcells[track_ind] in self.cells:
                                            pass
                                        else:
                                            self.cells[self.cells == other_div_cell] = prevcells[track_ind]
                                    

                            self.cells = self.cells[keep_cells]
                            self.track_indices = self.track_indices[keep_cells]
                            if len(self.div_indices > 0):
                                self.div_indices = self.div_indices[keep_div_cells]
                            self.div_track = self.div_track[keep_cells]

                            targets[0]['main']['cur_target']['track_query_hs_embeds'] = targets[0]['main']['cur_target']['track_query_hs_embeds'][keep_cells]
                            targets[0]['main']['cur_target']['track_query_boxes'] = targets[0]['main']['cur_target']['track_query_boxes'][keep_cells]

                            if 'track_query_masks' in targets[0]['main']['cur_target']:
                                targets[0]['main']['cur_target']['track_query_masks'] = targets[0]['main']['cur_target']['track_query_masks'][keep_cells]

                            mask_cells = np.unique(label_mask)
                            mask_cells = mask_cells[mask_cells != 0]

                        if ctc_data is None:
                            ctc_data = []
                            for cell in self.cells:
                                ctc_data.append(np.array([cell,i,i,0]))
                            ctc_data = np.stack(ctc_data)
                            ctc_cells = np.copy(self.cells)
                        else:
                            max_cellnb = ctc_data.shape[0]

                            ctc_cells_new = np.copy(self.cells)
                            mask_copy = np.copy(label_mask)

                            if prevcells is not None:
                                for c,cell in enumerate(prevcells):
                                    if cell in self.cells:
                                        ctc_cell = ctc_cells[c]
                                        if self.div_track[self.cells == cell] != -1:
                                            div_ind = self.div_track[self.cells == cell]
                                            div_cells = self.cells[self.div_track == div_ind]
                                            max_cellnb += 1
                                            new_cell_1 = np.array([max_cellnb,i,i,ctc_cell])[None]
                                            ctc_cells_new[self.cells == div_cells[0]] = max_cellnb 
                                            label_mask[mask_copy == div_cells[0]] = max_cellnb
                                            max_cellnb += 1
                                            new_cell_2 = np.array([max_cellnb,i,i,ctc_cell])[None]
                                            ctc_data = np.concatenate((ctc_data,new_cell_1,new_cell_2),axis=0)
                                            ctc_cells_new[self.cells == div_cells[1]] = max_cellnb
                                            label_mask[mask_copy == div_cells[1]] = max_cellnb
                                            assert div_cells[0] in mask_copy and div_cells[1] in mask_copy

                                        else:
                                            ctc_data[ctc_cell-1,2] = i
                                            ctc_cells_new[self.cells == cell] = ctc_cells[prevcells == cell]
                                            label_mask[mask_copy == cell] = ctc_cell
                                            # assert cell in mask_copy

                            for c,cell in enumerate(self.cells):
                                if prevcells is None or cell not in prevcells and self.div_track[c] == -1:
                                    max_cellnb += 1
                                    new_cell = np.array([max_cellnb,i,i,0])
                                    ctc_data = np.concatenate((ctc_data,new_cell[None]),axis=0)

                                    ctc_cells_new[self.cells == cell] = max_cellnb     
                                    label_mask[mask_copy == cell] = max_cellnb

                            ctc_cells = ctc_cells_new

                        if self.color_stack.shape[1] == 1 or self.color_stack.shape[2] == 1:
                            cv2.imwrite(str(self.output_dir / self.predictions_folder / f'mask{i:03d}.tif'),label_mask)
                        
                        if not self.resize:
                            self.label_masks[i,idx_y * len(self.loc_x) + idx_x,:,r*self.target_size[1]:(r+1)*self.target_size[1]] = label_mask       


                    if ((self.display_decoder_aux and i in random_nbs) or (self.display_all_aux_outputs and i > 0)):

                        if 'two_stage' in outputs:
                            enc_frame = np.array(img).copy()
                            enc_outputs = outputs['two_stage']
                            logits_topk = enc_outputs['pred_logits'][0,:,0].sigmoid()
                            boxes_topk = enc_outputs['pred_boxes'][0]
                            topk_proposals = enc_outputs['topk_proposals'][0]

                            enc_colors = np.array([(np.array([0.,0.,0.])) for _ in range(self.num_queries)])
                            used_object_queries = torch.where(outputs['pred_logits'][0,-self.num_queries:,0].sigmoid() > self.threshold)[0].sort()[0]
                            num_tracked_cells = len(self.cells) - len(used_object_queries)

                            if self.track:
                                if len(used_object_queries) > 0:
                                    counter = 0
                                    for pidx in used_object_queries:
                                            enc_colors[pidx] = self.all_colors[self.cells[num_tracked_cells+counter]-1]
                                            counter += 1
                                else:
                                    enc_colors = np.array([tuple((255*np.random.random(3))) for _ in range(logits_topk.shape[0])])
                            else:
                                enc_colors[:len(self.cells)] = self.all_colors[self.cells-1]

                            t0,t1,t2,t3 = 0.1,0.3,0.5,0.8
                            boxes_list = []
                            boxes_list.append(boxes_topk[logits_topk > t3])
                            boxes_list.append(boxes_topk[(logits_topk > t2) * (logits_topk < t3)])
                            boxes_list.append(boxes_topk[(logits_topk > t1) * (logits_topk < t2)])
                            boxes_list.append(boxes_topk[(logits_topk > t0) * (logits_topk < t1)])
                            boxes_list.append(boxes_topk[logits_topk < t0])

                            num_per_box = [box.shape[0] for box in boxes_list]
                            all_enc_boxes = []
                            enc_frames = []
                            for b,boxes in enumerate(boxes_list):
                                enc_frame = np.array(img).copy()
                                enc_frames.append(utils.plot_tracking_results(enc_frame,boxes,None,enc_colors[sum(num_per_box[:b]):sum(num_per_box[:b+1])],None,None))
                                all_enc_boxes.append(boxes[sum(num_per_box[:b]):sum(num_per_box[:b+1])])
                            
                            enc_frame = np.array(img).copy()
                            all_enc_boxes = torch.cat(boxes_list)
                            all_enc_boxes = torch.cat((all_enc_boxes[np.sum(enc_colors,-1) == 0],all_enc_boxes[np.sum(enc_colors,-1) > 0]))
                            enc_colors = np.concatenate((enc_colors[np.sum(enc_colors,-1) == 0],enc_colors[np.sum(enc_colors,-1) > 0]))
                            enc_frames.append(utils.plot_tracking_results(enc_frame,all_enc_boxes,None,enc_colors,None,None))

                            if len(used_object_queries) > 0:
                                enc_colors = np.array([(np.array([0.,0.,0.])) for _ in range(self.num_queries)])
                                enc_frames.append(utils.plot_tracking_results(enc_frame,all_enc_boxes,None,enc_colors,None,None))

                            enc_frames = np.concatenate((enc_frames),axis=1)

                            cv2.imwrite(str(self.output_dir / self.predictions_folder / 'two_stage' / (f'encoder_frame_{fp.stem}_{idx_y}_{idx_x}.png')),enc_frames)

                            if self.enc_map is not None:
                                #TODO update for future
                                proposals_img = np.array(img)
                                spatial_shapes = enc_outputs['spatial_shapes']

                                fmaps_cum_size = torch.tensor([spatial_shape[0] * spatial_shape[1] for spatial_shape in spatial_shapes]).cumsum(0)

                                proposals = topk_proposals[enc_outputs['pred_logits'][0,:,0].sigmoid() > 0.5].clone().cpu()
                                topk_proposals = topk_proposals.cpu()

                                for proposal in proposals:
                                    proposal_ind = torch.where(topk_proposals == proposal)[0][0]
                                    f = 0
                                    fmap = fmaps_cum_size[f]
                                    proposal_clone = proposal.clone()

                                    while proposal_clone >= fmap and f < len(fmaps_cum_size) - 1:
                                        f += 1 
                                        fmap = fmaps_cum_size[f]

                                    if f > 0:
                                        proposal_clone -= fmaps_cum_size[f-1]

                                    y = torch.floor_divide(proposal_clone,spatial_shapes[f,1])
                                    x = torch.remainder(proposal_clone,spatial_shapes[f,1])

                                    self.enc_map[f][y,x] += 1
                                    
                                    small_mask = np.zeros(spatial_shapes[f].cpu().numpy(),dtype=np.uint8)
                                    small_mask[y,x] += 1

                                    resize_mask = cv2.resize(small_mask,self.target_size[::-1])

                                    y, x = np.where(resize_mask == 1)

                                    X = np.min(x) 
                                    Y = np.min(y)
                                    width = (np.max(x) - np.min(x)) 
                                    height = (np.max(y) - np.min(y)) 

                                    if len(used_object_queries) > 0:
                                        proposals_img = cv2.rectangle(proposals_img,(X,Y),(int(X+width),int(Y+height)),color=self.all_colors[self.cells[self.track_indices == proposal_ind]-1],thickness=1)
                                    else:
                                        proposals_img = cv2.rectangle(proposals_img,(X,Y),(int(X+width),int(Y+height)),color=enc_colors[proposal == topk_proposals][0],thickness=1)


                                cv2.imwrite(str(self.output_dir / self.predictions_folder / 'two_stage' / (f'encoder_frame_{fp.stem}_proposals_{idx_y}_{idx_x}.png')),proposals_img)

                        references = outputs['references'].detach()

                        if references.shape[-1] == 2:
                            assert self.use_dab == False
                            references = torch.cat((references, torch.zeros(references.shape[:-1] + (6,),device=references.device)),axis=-1)

                        aux_outputs = outputs['aux_outputs'] # output from the intermedaite layers of decoder
                        aux_outputs = [{'pred_boxes':references[0]}] + aux_outputs # add the initial anchors / reference points

                        if 'pred_masks' in outputs:
                            aux_outputs.append({'pred_boxes':outputs['pred_boxes'].detach(),'pred_masks':outputs['pred_masks'].detach(),'pred_logits':outputs['pred_logits'].detach()}) # add the last layer of the decoder which is the final prediction
                        else:
                            aux_outputs.append({'pred_boxes':outputs['pred_boxes'].detach(),'pred_logits':outputs['pred_logits'].detach()}) # add the last layer of the decoder which is the final prediction

                        img = np.array(img)

                        cells_exit_ids = torch.tensor([[cidx,c] for cidx,c in enumerate(prevcells.astype(np.int64)) if c not in self.cells]) if prevcells is not None else None

                        enc_oqs_indices = torch.where(outputs['two_stage']['pred_logits'][0,:,0].sigmoid() > 0.5)[0] + (len(keep) - self.num_queries)

                        if self.track:
                            previmg_copy = previmg.copy()
                            img_box = img.copy()
                            img_mask = img.copy()
                            img_enc_oqs = img.copy()
                        for a,aux_output in enumerate(aux_outputs):
                            all_boxes = aux_output['pred_boxes'][0].detach()
                            img_copy = img.copy()
                            
                            if self.track_indices is not None:
                                tq_ind_only = self.track_indices < (len(keep) - self.num_queries)
                                track_indices_tq_only = self.track_indices[tq_ind_only]
                                div_track = self.div_track[tq_ind_only]                        
                            else:
                                track_indices_tq_only = None
                            
                            if a > 0:
                                all_logits = aux_output['pred_logits'][0].detach()

                                object_boxes = all_boxes[-self.num_queries:,:4]
                                object_logits = all_logits[-self.num_queries:,0]
                                object_indices = torch.where(object_logits > 0.5)[0]
                                pred_object_boxes = object_boxes[object_indices]
                                
                                pred_enc_oqs_boxes = all_boxes[enc_oqs_indices]

                                if self.masks:
                                    if self.return_intermediate_masks or a == len(aux_outputs) - 1:
                                        all_masks = aux_output['pred_masks'][0].detach()
                                        object_masks = all_masks[-self.num_queries:,0]
                                        pred_object_masks = object_masks[object_indices]
                                    else:
                                        pred_object_masks = None

                                object_indices += (len(keep) - self.num_queries)

                                if len(object_indices) > 0:
                                    if self.masks:
                                        img_object = utils.plot_tracking_results(img_copy,pred_object_boxes,pred_object_masks,self.all_colors[-object_indices.cpu()],None,None)
                                    else:
                                        img_object = utils.plot_tracking_results(img_copy,pred_object_boxes,None,self.all_colors[-object_indices.cpu()],None,None)
                                else:
                                    img_object = utils.plot_tracking_results(img_copy,pred_enc_oqs_boxes,None,self.all_colors[-enc_oqs_indices.cpu()],None,None)

                            if track_indices_tq_only is not None:
                                
                                if a > 0: # initial reference points are all single boxes; this applies to the outputs of decoder
                                    if len(track_indices_tq_only) > 0:
                                        track_boxes = all_boxes[track_indices_tq_only]
                                        track_logits = all_logits[track_indices_tq_only]
                                        if self.masks and (self.return_intermediate_masks or a == len(aux_outputs) - 1):
                                            track_masks = all_masks[track_indices_tq_only]

                                        unique_divs = np.unique(div_track[div_track != -1])
                                        for unique_div in unique_divs:
                                            div_ids = (div_track == unique_div).nonzero()[0]
                                            track_boxes[div_ids[1],:4] = track_boxes[div_ids[0],4:]
                                            track_logits[div_ids[1],:1] = track_logits[div_ids[0],1:]
                                            if self.masks and (self.return_intermediate_masks or a == len(aux_outputs) - 1):
                                                track_masks[div_ids[1],:1] = track_masks[div_ids[0],1:]

                                        track_boxes = track_boxes[:,:4]
                                        track_logits = track_logits[:,0]

                                        if self.masks and (self.return_intermediate_masks or a == len(aux_outputs) - 1):
                                            track_masks = track_masks[:,0]
                                        else:
                                            track_masks = None

                                        track_box_colors = self.all_colors[self.cells-1]
                                        new_cells = self.new_cells if self.track else None
                                    else:
                                        track_boxes = torch.zeros((0,4),dtype=all_boxes.dtype,device=all_boxes.device)

                                elif a == 0:
                                    track_masks = None
                                    track_boxes = all_boxes[np.arange(len(keep) - self.num_queries)]
                                    object_boxes = all_boxes[-self.num_queries:]
                                    track_box_colors = self.all_colors[prevcells-1] if prevcells is not None else self.all_colors[self.cells-1]
                                    object_box_colors = np.array([(np.array([0.,0.,0.])) for _ in range(self.num_queries)])
                                    div_track = np.ones((track_boxes.shape[0])) * -1
                                    new_cells = None

                                    pred_enc_oqs_boxes = all_boxes[enc_oqs_indices]

                                    all_black = object_box_colors.copy()

                                    if self.track:
                                        assert track_boxes.shape[0] <= track_box_colors.shape[0]
                                        previmg_track_anchor_boxes = utils.plot_tracking_results(previmg_copy,track_boxes,None,track_box_colors,div_track,new_cells)

                                        all_colors = np.concatenate((object_box_colors,track_box_colors),axis=0)
                                        all_boxes_rev = torch.cat((object_boxes,track_boxes),axis=0)
                                        previmg_all_anchor_boxes = utils.plot_tracking_results(img_copy,all_boxes_rev,None,all_colors,None,None)

                                    else:
                                        object_box_colors[-track_box_colors.shape[0]:] = track_box_colors
                                        if self.two_stage:
                                            object_boxes = torch.cat((object_boxes[track_box_colors.shape[0]:],object_boxes[:track_box_colors.shape[0]]))
                                        else:
                                            unused_oqs = np.array([oq_id for oq_id in range(self.num_queries) if oq_id not in track_indices_tq_only])
                                            object_boxes = torch.cat((object_boxes[unused_oqs],object_boxes[track_indices_tq_only]))

                                    img_object_anchor_boxes_all = utils.plot_tracking_results(img_copy,object_boxes,None,object_box_colors,None,None)
                                    img_object_anchor_boxes_not_track_only = utils.plot_tracking_results(img_copy,object_boxes,None,all_black,None,None)

                                    oq_indices = []
                                    for q in range(len(keep)):
                                        if q not in track_indices_tq_only:
                                            if q < (len(keep) - self.num_queries):
                                                oq_indices.append(q)
                                            else:
                                                oq_indices.append(-q)
                                    oq_colors = self.all_colors[oq_indices]
                                    oq_indices = [abs(oq_ind) for oq_ind in oq_indices]
                                    img_object_anchor_boxes_not_track_color_only = utils.plot_tracking_results(img_copy,all_boxes[oq_indices],None,oq_colors,None,None)

                                    img_enc_oqs = utils.plot_tracking_results(img_copy,pred_enc_oqs_boxes,None,self.all_colors[-enc_oqs_indices.cpu()],None,None)

                                    if not self.track:
                                        img_object_anchor_boxes_track_only = utils.plot_tracking_results(img_copy,object_boxes[-len(track_indices_tq_only):],None,track_box_colors,None,None)
                                        img_object_anchor_boxes = np.concatenate((img_object_anchor_boxes_all,img_object_anchor_boxes_not_track_only,img_object_anchor_boxes_track_only,img_object_anchor_boxes_not_track_color_only),axis=1)
                                    else:
                                        img_object_anchor_boxes = np.concatenate((img_object_anchor_boxes_all,img_object_anchor_boxes_not_track_only,img_object_anchor_boxes_not_track_color_only,img_enc_oqs),axis=1)

                                    if i == 1:
                                        all_prev_logits = prev_outputs['pred_logits'][0].detach().sigmoid()
                                        all_prev_boxes = prev_outputs['pred_boxes'][0]

                                        prevkeep = all_prev_logits[:,0] > self.threshold
                                        prev_boxes = all_prev_boxes[prevkeep]
                                        prev_colors = self.all_colors[prevcells-1]

                                        if self.masks:
                                            all_prev_masks = prev_outputs['pred_masks'][0,:,0].detach().sigmoid().numpy()
                                            prev_masks = all_prev_masks[prevkeep]

                                        first_img_box = utils.plot_tracking_results(previmg_copy,prev_boxes,None,prev_colors,None,None)

                                        if self.masks:
                                            first_img_mask = utils.plot_tracking_results(previmg_copy,None,prev_masks,prev_colors,None,None)
                                            first_img_box_and_mask = utils.plot_tracking_results(previmg_copy,prev_boxes,prev_masks,prev_colors,None,None)

                                            img_object_anchor_boxes = np.concatenate((first_img_box,first_img_mask,first_img_box_and_mask,img_object_anchor_boxes),axis=1)
                                        else:
                                            img_object_anchor_boxes = np.concatenate((first_img_box,img_object_anchor_boxes),axis=1)

                                img_track = utils.plot_tracking_results(img_copy,track_boxes,track_masks,track_box_colors,div_track,new_cells)

                                if a == len(aux_outputs) - 1:
                                    img_box = utils.plot_tracking_results(img_box,track_boxes,None,track_box_colors,div_track,new_cells)
                                    img_mask = utils.plot_tracking_results(img_mask,None,track_masks,track_box_colors,div_track,new_cells)

                            else:
                                track_boxes = torch.zeros((0,4),dtype=all_boxes.dtype,device=all_boxes.device)
                                img_track = img_copy
                            
                        
                            if a == 0 and self.track:
                                if track_indices_tq_only is not None:
                                    decoder_frame = np.concatenate((previmg,img,previmg_all_anchor_boxes,img_object_anchor_boxes,previmg_track_anchor_boxes,img_track),axis=1)
                                else:
                                    decoder_frame = np.concatenate((previmg,img,img_track,previmg_all_anchor_boxes),axis=1)
                            elif a == 0:
                                decoder_frame = np.concatenate((img,img_track,img_object_anchor_boxes),axis=1)
                            else:
                                decoder_frame = np.concatenate((decoder_frame,img_track,img_object),axis=1)

                            if a == len(aux_outputs)-1:
                                if self.masks:
                                    decoder_frame = np.concatenate((decoder_frame,img_box,img_mask),axis=1)
                                else:
                                    decoder_frame = np.concatenate((decoder_frame,img_box),axis=1)

                            # Plot all predictions regardless of cls label
                            if a == len(aux_outputs) - 1:
                                img_copy = img.copy()
                                
                                color_queries = np.array([(np.array([0.,0.,0.])) for _ in range(self.num_queries)])

                                if  cells_exit_ids is not None and cells_exit_ids.shape[0] > 0: # Plot the track query that left the chamber
                                    boxes_exit = all_boxes[cells_exit_ids[:,0],:4]
                                    boxes = torch.cat((all_boxes[-self.num_queries:,:4],boxes_exit,track_boxes))
                                    colors_prev = self.all_colors[cells_exit_ids[:,1] - 1] 
                                    colors_prev = colors_prev[None] if colors_prev.ndim == 1 else colors_prev
                                    
                                    all_colors = np.concatenate((color_queries,colors_prev,self.all_colors[self.cells-1]),axis=0)
                                    div_track_all = np.ones((boxes.shape[0])) * -1 # all boxes does not contain div boxes separated
                                    if len(track_boxes) > 0:
                                        div_track_all[-len(track_boxes):] = div_track

                                    assert len(div_track_all) == len(boxes)

                                else: # all cells / track queries stayed in the chamber
                                    boxes = torch.cat((all_boxes[-self.num_queries:,:4],track_boxes))
                                    all_colors = np.concatenate((color_queries,self.all_colors[self.cells-1]),axis=0)
                                    div_track_all = np.concatenate((np.ones((self.num_queries))*-1,div_track))

                                    assert len(div_track_all) == len(boxes)

                                new_cell_thickness = np.zeros_like(div_track_all).astype(bool)
                                if cells_exit_ids is not None and cells_exit_ids.shape[0] > 0:
                                    new_cell_thickness[self.num_queries:-track_boxes.shape[0]] = False
                                img_final_box = utils.plot_tracking_results(img_copy,boxes,None,all_colors,div_track_all,new_cell_thickness)

                                color_black = np.array([(np.array([0.,0.,0.])) for _ in range(boxes.shape[0])]) # plot all bounding boxes as black
                                img_final_all_box = utils.plot_tracking_results(img_copy,boxes,None,color_black,None,None)

                                if  cells_exit_ids is not None and cells_exit_ids.shape[0] > 0: # Plot the track query that left the chamber
                                    all_colors[len(color_queries):-len(self.cells)] = np.array([0.,0.,0.])
                                    img_final_all_box = utils.plot_tracking_results(img_copy,boxes,None,all_colors,None,None)

                                oq_indices = np.array([q for q in range(len(keep)) if track_indices_tq_only is None or q not in track_indices_tq_only])
                                oq_colors = self.all_colors[-oq_indices]
                                img_oq_only_color = utils.plot_tracking_results(img_copy,all_boxes[oq_indices,:4],None,oq_colors,None,None)
                                
                                decoder_frame = np.concatenate((decoder_frame,img_final_box,img_final_all_box,img_oq_only_color),axis=1)

                        if self.args.CoMOT:
                            img_CoMOT_color = img.copy()
                            pred_logits_aux = outputs['aux_outputs'][-2]['pred_logits'][0,-self.num_queries:,0].sigmoid().cpu().numpy()
                            aux_object_ind = np.where(pred_logits_aux > 0.5)[0] + (len(keep) - self.num_queries)

                            if len(aux_object_ind) > 0:
                                aux_boxes = outputs['aux_outputs'][-2]['pred_boxes'][0,aux_object_ind,:4]

                                if self.masks and self.return_intermediate_masks:
                                    aux_masks = outputs['aux_outputs'][-2]['pred_masks'][0,aux_object_ind,0]
                                else:
                                    aux_masks = None

                                img_CoMOT_color = utils.plot_tracking_results(img_CoMOT_color,aux_boxes,aux_masks,self.all_colors[-aux_object_ind],None,None)

                            decoder_frame = np.concatenate((decoder_frame,img_CoMOT_color),axis=1)

                        else:

                            if 'two_stage' in outputs:

                                img_top_oq_last = img.copy()
                                img_top_oq_first = img.copy()
    
                                top_oq_ind = np.where(outputs['two_stage']['pred_logits'][0,:,0].sigmoid().cpu().numpy() > 0.5)[0] + (len(keep) - self.num_queries)

                                top_oq_boxes_last = outputs['aux_outputs'][-1]['pred_boxes'][0, + top_oq_ind,:4]
                                top_oq_boxes_first = outputs['aux_outputs'][0]['pred_boxes'][0, + top_oq_ind,:4]

                                img_top_oq_last = utils.plot_tracking_results(img_top_oq_last,top_oq_boxes_last,None,self.all_colors[-len(top_oq_ind):],None,None)
                                img_top_oq_first = utils.plot_tracking_results(img_top_oq_first,top_oq_boxes_first,None,self.all_colors[-len(top_oq_ind):],None,None)

                                decoder_frame = np.concatenate((decoder_frame,img_top_oq_first,img_top_oq_last),axis=1)


                        method = 'object_detection' if not self.track else 'track'
                        cv2.imwrite(str(self.output_dir / self.predictions_folder / 'decoder_bbox_outputs' / (f'{method}_decoder_frame_{fp.stem}_{idx_y}_{idx_x}.png')),decoder_frame)

                    else:
                        if 'two_stage' in outputs and self.enc_map is not None:
                            
                            enc_outputs = outputs['two_stage']
                            topk_proposals = enc_outputs['topk_proposals'][0]
                            spatial_shapes = enc_outputs['spatial_shapes']

                            fmaps_cum_size = torch.tensor([spatial_shape[0] * spatial_shape[1] for spatial_shape in spatial_shapes]).cumsum(0)

                            # proposals = topk_proposals[enc_outputs['pred_logits'][0,topk_proposals,0].sigmoid() > 0.5].clone()

                            for proposal in topk_proposals:
                                f = 0
                                fmap = fmaps_cum_size[f]

                                while proposal >= fmap and f < len(fmaps_cum_size) - 1:
                                    f += 1 
                                    fmap = fmaps_cum_size[f]

                                if f > 0:
                                    proposal -= fmaps_cum_size[f-1]

                                y = torch.floor_divide(proposal,spatial_shapes[f,1])
                                x = torch.remainder(proposal,spatial_shapes[f,1])

                                self.enc_map[f][y,x] += 1

                    torch.cuda.empty_cache()
                        
                    if sum(keep) == 0:
                        prevcells = None

                    prev_outputs = outputs.copy()
                    previmg = img.copy()

                self.res_tracks[idx_y,idx_x] = ctc_data

                # if ctc_data is not None and not self.resize:

                #     index = idx_y * len(self.loc_x) + idx_x

                #     for framenb in range(self.label_masks.shape[0]):
                #         cellnbs = np.unique(self.label_masks[framenb,index])
                #         cellnbs = cellnbs[cellnbs!=0]
                #         for cellnb in cellnbs:
                #             mask = (self.label_masks[framenb,index] == cellnb).astype(np.uint8)

                #             y,x = np.where(mask[:,:]>0)
                #             median_y = y[np.argmin(np.abs(y - np.median(y)))]
                #             ind_y = np.where(y == median_y)[0]

                #             ind_x = np.argmin(np.abs(x[ind_y] - np.median(x[ind_y])))               
                #             ind = ind_y[ind_x]
                #             org = (x[ind] - len(str(int(cellnb))) * 3,y[ind])

                #             self.color_stack[framenb,idx_y,idx_x] = cv2.putText(
                #                 self.color_stack[framenb,idx_y,idx_x],
                #                 text = f'{int(cellnb)}', 
                #                 org=org, 
                #                 fontFace=cv2.FONT_HERSHEY_SIMPLEX, 
                #                 fontScale = 0.4,
                #                 color = (255,255,255),
                #                 thickness=1,
                #                 )

                #     crf = 20
                #     verbose = 1

                #     filename = self.output_dir / self.predictions_folder / (f'movie_{idx_y}_{idx_x}.mp4')                

                #     height, width, _ = self.color_stack[0,idx_y,idx_x].shape
                #     if height % 2 == 1:
                #         height -= 1
                #     if width % 2 == 1:
                #         width -= 1
                #     quiet = [] if verbose else ["-loglevel", "error", "-hide_banner"]
                #     process = (
                #         ffmpeg.input(
                #             "pipe:",
                #             format="rawvideo",
                #             pix_fmt="rgb24",
                #             s="{}x{}".format(width, height),
                #             r=7,
                #         )
                #         .output(
                #             str(filename),
                #             pix_fmt="yuv420p",
                #             vcodec="libx264",
                #             crf=crf,
                #             preset="veryslow",
                #         )
                #         .global_args(*quiet)
                #         .overwrite_output()
                #         .run_async(pipe_stdin=True)
                #     )

                #     # Write frames:
                #     for frame in self.color_stack[:,idx_y,idx_x]:
                #         process.stdin.write(frame[:height, :width].astype(np.uint8).tobytes())

                #     # Close file stream:
                #     process.stdin.close()

                #     # Wait for processing + close to complete:
                #     process.wait()

        start_time = time.time()

        if self.color_stack.shape[1] > 1 or self.color_stack.shape[2] > 1:
            ctc_data, movie = self.stitch_pic(self.label_masks,fps_ROI)
            self.color_stack = movie
        else:
            self.color_stack = self.color_stack[:,0,0]

        print(f'{time.time() - start_time} seconds')

        if 'two_stage' in outputs:

            spacer = 1
            scale = self.target_size[0] / self.enc_map[0].shape[0]
            enc_maps = []
            max_value = 0
            for e,enc_map in enumerate(self.enc_map):

                enc_map = cv2.resize(enc_map,(self.target_size[1],self.target_size[0]),interpolation=cv2.INTER_NEAREST)
                enc_map = np.repeat(enc_map[:,:,None],3,-1)
                max_value = max(max_value,np.max(enc_map))
                enc_maps.append(enc_map)
                border = np.zeros((self.target_size[0],spacer,3),dtype=np.uint8)
                border[:,:,0] = -1
                enc_maps.append(border)

            enc_maps = np.concatenate((enc_maps),axis=1)

            img_empty = cv2.imread(str(self.output_dir.parents[4] / 'examples' / 'empty_chamber.png'))

            enc_maps[enc_maps!=-1] = (enc_maps[enc_maps!=-1] / max_value) * 255
            enc_maps[enc_maps==-1] = 255
            enc_maps = enc_maps[:,:-spacer]

            if img_empty is not None:
                enc_maps = np.concatenate((img_empty,enc_maps[:,self.target_size[1]:self.target_size[1]+spacer],enc_maps),axis=1)
            else:
                enc_maps = np.concatenate((enc_maps[:,self.target_size[1]:self.target_size[1]+spacer],enc_maps),axis=1)

            cv2.imwrite(str(self.output_dir / self.predictions_folder / 'enc_queries_picked.png'),enc_maps.astype(np.uint8))

        if ctc_data is not None and self.masks:
            np.savetxt(self.output_dir / self.predictions_folder / 'res_track.txt',ctc_data,fmt='%d')

        if self.write_video:
            crf = 20
            verbose = 1

            filename = self.output_dir / self.predictions_folder / (f'movie_full.mp4') 

            assert self.color_stack.max() <= 255.0 and self.color_stack.max() >= 0.0               

            print(filename)
            height, width, _ = self.color_stack[0].shape
            if height % 2 == 1:
                height -= 1
            if width % 2 == 1:
                width -= 1
            quiet = [] if verbose else ["-loglevel", "error", "-hide_banner"]
            process = (
                ffmpeg.input(
                    "pipe:",
                    format="rawvideo",
                    pix_fmt="rgb24",
                    s="{}x{}".format(width, height),
                    r=7,
                )
                .output(
                    str(filename),
                    pix_fmt="yuv420p",
                    vcodec="libx264",
                    crf=crf,
                    preset="veryslow",
                )
                .global_args(*quiet)
                .overwrite_output()
                .run_async(pipe_stdin=True)
            )

            # Write frames:
            for frame in self.color_stack:
                process.stdin.write(frame[:height, :width].astype(np.uint8).tobytes())

            # Close file stream:
            process.stdin.close()

            # Wait for processing + close to complete:
            process.wait()

        if self.display_object_query_boxes:

            scale = 8
            wspacer = 5 * scale
            hspacer = 20 * scale

            max_area = [np.max(boxes[:,2] * boxes[:,3]) for boxes in self.query_box_locations]
            num_boxes_used = np.sum(np.array(max_area) > 0)
            query_frames = np.ones((self.target_size[0]*scale + hspacer, (self.target_size[1]*scale + wspacer) * num_boxes_used,3),dtype=np.uint8) * 255
            where_boxes = np.where(np.array(max_area) > 0)[0]

            for j,ind in enumerate(where_boxes):
                img_empty = cv2.imread(str(self.output_dir.parents[1] / 'examples' / 'empty_chamber.png'))
                img_empty = cv2.resize(img_empty,(self.target_size[1]*scale,self.target_size[0]*scale))
            
                for box in self.query_box_locations[ind][1:]:
                    img_empty = cv2.circle(img_empty, (int(box[0]*scale),int(box[1]*scale)), radius=1*scale, color=(255,0,0), thickness=-1)

                img_empty = np.concatenate((np.ones((hspacer,self.target_size[1]*scale,3),dtype=np.uint8)*255,img_empty),axis=0)
                shift = 5 if ind + 1 >= 10 else 12
                img_empty = cv2.putText(img_empty,f'{ind+1}',org=(shift*scale,15*scale),fontFace=cv2.FONT_HERSHEY_COMPLEX,fontScale=4,color=(0,0,0),thickness=4)
                query_frames[:,j*(self.target_size[1]*scale+wspacer): j*(self.target_size[1]*scale+wspacer) + self.target_size[1]*scale] = img_empty

            cv2.imwrite(str(self.output_dir / self.predictions_folder / (f'{method}_object_query_box_locations.png')),query_frames)

            if self.use_dab:
                height,width = self.target_size[0] * scale, self.target_size[1] * scale
                boxes = aux_outputs[0]['pred_boxes'][0,:,:4].detach().cpu().numpy()

                boxes[:,1::2] = boxes[:,1::2] * height
                boxes[:,::2] = boxes[:,::2] * width

                boxes[:,0] = boxes[:,0] - boxes[:,2] // 2
                boxes[:,1] = boxes[:,1] - boxes[:,3] // 2

                for j,ind in enumerate(where_boxes):

                        bounding_box = boxes[ind]

                        query_frames = cv2.rectangle(
                        query_frames,
                        (int(np.clip(bounding_box[0],0,width)) + j * (width + wspacer), int(np.clip(bounding_box[1],0,height))+hspacer),
                        (int(np.clip(bounding_box[0] + bounding_box[2],0,width)) + j * (width + wspacer), int(np.clip(bounding_box[1] + bounding_box[3],0,height))+hspacer),
                        color=tuple(np.array([50.,50.,50.])),
                        thickness = 5)

                cv2.imwrite(str(self.output_dir / self.predictions_folder / (f'{method}_object_query_box_locations_with_boxes.png')),query_frames)